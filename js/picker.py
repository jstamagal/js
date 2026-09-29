"""prompt_toolkit provider/model picker used inside the js REPL.

The CLI login flow owns credentials and model discovery. This picker only
chooses from saved provider logins and their cached model lists.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
import sys
from typing import Any

from prompt_toolkit.application import Application, in_terminal
from prompt_toolkit.application.current import get_app_or_none
from prompt_toolkit.data_structures import Point
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.input import Input, create_input
from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent
from prompt_toolkit.layout import HSplit, Layout, VSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.output import Output, create_output
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import Frame

from . import logins, providers
from . import messages as msgs


@dataclass(frozen=True)
class ProviderRow:
    id: str
    name: str
    source: str
    provider_base_url: str | None = None
    provider_api_key: str | None = None
    provider_headers: dict[str, str] | None = None

@dataclass(frozen=True)
class ModelRow:
    id: str
    provider: str


def _provider_rows() -> list[ProviderRow]:
    saved = logins.load_logins()
    rows: list[ProviderRow] = []

    for provider_id, login in sorted(saved.items()):
        provider = providers.get_provider(provider_id)
        rows.append(
            ProviderRow(
                id=provider_id,
                name=provider.display_name if provider else provider_id,
                source="login",
                provider_base_url=login.provider_base_url,
                provider_api_key=login.provider_api_key,
                provider_headers=dict(login.provider_headers),
            )
        )
    rows.sort(key=lambda row: row.name.lower())
    return rows


def _model_rows(provider_id: str) -> list[ModelRow]:
    cached = logins.load_model_cache().get(provider_id)
    if not cached:
        return []
    return [ModelRow(id=model_id, provider=provider_id) for model_id in cached]


# Colors are the terminal's: `ansi*` names resolve through whatever palette the
# terminal runs, and nothing paints a background, so the picker sits in the
# same colors as the shell around it.
_STYLE = Style.from_dict(
    {
        "frame.border": "ansiblue",
        "frame.label": "ansicyan bold",
        "row.selected": "bg:ansigreen fg:ansiblack bold",
        "row.marked": "bold",
        "row.dim": "ansibrightblack",
        "detail": "ansigreen",
        "help": "ansigreen",
    }
)



class ModelPicker:
    """Two-pane provider/model picker: state plus the keys that drive it."""

    def __init__(
        self,
        *,
        provider_id: str | None = None,
        provider_base_url: str | None = None,
        provider_api_key: str | None = None,
        model: str | None = None,
    ) -> None:
        self.provider_rows = _provider_rows()
        self.model_rows: list[ModelRow] = []
        self.provider_index = 0
        self.model_index = 0
        self.focus = "providers"
        self.detail = ""
        self._initial_model = model
        self._override_provider_id = (
            providers.normalize_provider_id(provider_id) if provider_id else None
        )
        self._override_base_url = provider_base_url
        self._override_api_key = provider_api_key
        for idx, row in enumerate(self.provider_rows):
            if row.id == self._override_provider_id:
                self.provider_index = idx
                break
        self._load_models()

    # -- state -------------------------------------------------------------

    def _load_models(self) -> None:
        self.model_index = 0
        if not self.provider_rows:
            self.model_rows = []
            self.detail = msgs.PICK_NO_LOGINS.text()
            return
        provider = self.provider_rows[self.provider_index]
        self.model_rows = _model_rows(provider.id)
        if not self.model_rows:
            self.detail = msgs.PICK_NO_CACHED.text(provider=provider.id)
            return
        if self._initial_model:
            for idx, row in enumerate(self.model_rows):
                if row.id == self._initial_model:
                    self.model_index = idx
                    break
        self.detail = msgs.PICK_PROVIDER_DETAIL.text(provider=provider.id, source=provider.source,
                                                     models=msgs.plural(len(self.model_rows), "model"))

    def current_provider(self) -> ProviderRow | None:
        if not self.provider_rows:
            return None
        return self.provider_rows[self.provider_index]

    def _provider_selection_values(self, provider: ProviderRow) -> tuple[str | None, str | None]:
        if provider.id == self._override_provider_id:
            base_url = self._override_base_url if self._override_base_url is not None else provider.provider_base_url
            api_key = self._override_api_key if self._override_api_key is not None else provider.provider_api_key
            return base_url, api_key
        return provider.provider_base_url, provider.provider_api_key

    def move(self, delta: int) -> None:
        if self.focus == "providers":
            if not self.provider_rows:
                return
            index = max(0, min(len(self.provider_rows) - 1, self.provider_index + delta))
            if index != self.provider_index:
                self.provider_index = index
                self._load_models()
        elif self.model_rows:
            self.model_index = max(0, min(len(self.model_rows) - 1, self.model_index + delta))

    def toggle_focus(self) -> None:
        self.focus = "models" if self.focus == "providers" else "providers"

    def selection(self) -> dict[str, Any] | None:
        """The chosen route, or None when there is no model to choose."""
        provider = self.current_provider()
        if provider is None or not self.model_rows:
            return None
        model = self.model_rows[self.model_index]
        base_url, api_key = self._provider_selection_values(provider)
        return {
            "provider_id": provider.id,
            "provider_base_url": base_url,
            "provider_api_key": api_key,
            "provider_headers": dict(provider.provider_headers or {}),
            "model": model.id,
        }

    async def fetch(self) -> None:
        """Refresh the current provider's model list from its API into the cache."""
        provider = self.current_provider()
        if provider is None:
            return
        login = logins.load_logins().get(provider.id)
        if login is None:
            self.detail = msgs.PICK_NOT_LOGGED_IN.text(provider=provider.id)
            return
        base_url, api_key = self._provider_selection_values(provider)
        if base_url is not None or api_key is not None:
            login = replace(
                login,
                provider_base_url=base_url if base_url is not None else login.provider_base_url,
                provider_api_key=api_key if api_key is not None else login.provider_api_key,
            )
        try:
            models = await logins.fetch_models(login)
            logins.cache_models(provider.id, models)
            self._load_models()
        except Exception as exc:  # noqa: BLE001
            self.detail = msgs.PICK_FETCH_FAILED.text(error=f"{type(exc).__name__}: {exc}")

    # -- view --------------------------------------------------------------

    def _rows_text(self, pane: str) -> StyleAndTextTuples:
        if pane == "providers":
            labels = [msgs.PICK_PROVIDER_ROW.text(provider=row.id, name=row.name) for row in self.provider_rows]
            index = self.provider_index
        else:
            labels = [row.id for row in self.model_rows]
            index = self.model_index
            if not labels:
                empty = (msgs.PICK_NO_MODELS if self.provider_rows else msgs.PICK_NO_LOGINS).text()
                return [("class:row.dim", empty)]
        focused = self.focus == pane
        out: StyleAndTextTuples = []
        for idx, label in enumerate(labels):
            if idx == index:
                style = "class:row.selected" if focused else "class:row.marked"
                out.append((style, f"{'▸' if not focused else ' '} {label} "))
            else:
                out.append(("", f"  {label}"))
            out.append(("", "\n"))
        return out

    def _pane(self, pane: str, title: str, width: Dimension | None) -> Frame:
        control = FormattedTextControl(
            lambda: self._rows_text(pane),
            get_cursor_position=lambda: Point(
                0, self.provider_index if pane == "providers" else self.model_index
            ),
            show_cursor=False,
        )
        return Frame(Window(control, always_hide_cursor=True), title=title, width=width)

    def application(
        self, *, input: Input | None = None, output: Output | None = None
    ) -> Application[dict[str, Any] | None]:
        kb = KeyBindings()

        @kb.add("up")
        @kb.add("k")
        def _up(event: KeyPressEvent) -> None:
            self.move(-1)

        @kb.add("down")
        @kb.add("j")
        def _down(event: KeyPressEvent) -> None:
            self.move(1)

        @kb.add("pageup")
        def _page_up(event: KeyPressEvent) -> None:
            self.move(-10)

        @kb.add("pagedown")
        def _page_down(event: KeyPressEvent) -> None:
            self.move(10)

        @kb.add("tab")
        @kb.add("left")
        @kb.add("right")
        def _toggle(event: KeyPressEvent) -> None:
            self.toggle_focus()

        @kb.add("enter")
        def _choose(event: KeyPressEvent) -> None:
            if self.focus == "providers":
                if self.model_rows:
                    self.focus = "models"
                return
            chosen = self.selection()
            if chosen is not None:
                event.app.exit(result=chosen)

        @kb.add("escape", eager=True)
        @kb.add("q")
        @kb.add("c-c")
        def _quit(event: KeyPressEvent) -> None:
            event.app.exit(result=None)

        @kb.add("f")
        def _fetch(event: KeyPressEvent) -> None:
            self.detail = msgs.PICK_FETCHING.text()

            async def run() -> None:
                await self.fetch()
                event.app.invalidate()

            event.app.create_background_task(run())

        body = VSplit(
            [
                self._pane("providers", msgs.PICK_PROVIDERS.text(), Dimension.exact(38)),
                self._pane("models", msgs.PICK_MODELS.text(), None),
            ],
            padding=1,
        )
        root = HSplit(
            [
                body,
                Window(FormattedTextControl(lambda: [("class:detail", self.detail)]), height=1),
                Window(FormattedTextControl([("class:help", msgs.PICK_HELP.text())]), height=1),
            ]
        )
        return Application(
            layout=Layout(root),
            key_bindings=kb,
            style=_STYLE,
            full_screen=True,
            input=input,
            output=output,
        )


def pick_model(
    *,
    provider_id: str | None = None,
    provider_base_url: str | None = None,
    provider_api_key: str | None = None,
    model: str | None = None,
) -> dict[str, Any] | None:
    """Run the interactive picker and return the selection, or None on cancel.
    See `run_modal` for how it shares the terminal with the REPL."""
    state = ModelPicker(
        provider_id=provider_id,
        provider_base_url=provider_base_url,
        provider_api_key=provider_api_key,
        model=model,
    )
    app = state.application(
        input=create_input(sys.__stdin__),
        output=create_output(sys.__stdout__),
    )
    return run_modal(app)


def run_modal(app: Application[Any]) -> Any:
    """Run a full-screen picker app to its result, blocking.

    Under the non-blocking REPL this is called from an executor thread while
    the REPL's own prompt_toolkit app runs on the loop; the picker then runs on
    that loop inside ``in_terminal()``, which detaches the REPL's input and
    redraws it after, so the two apps never read the terminal at once.
    """
    host = get_app_or_none()
    host_loop = getattr(host, "loop", None) if host is not None and host.is_running else None
    if host_loop is None:
        return app.run()
    try:
        on_host_loop = asyncio.get_running_loop() is host_loop
    except RuntimeError:
        on_host_loop = False
    if on_host_loop:
        raise RuntimeError("run_modal() blocks; call it off the REPL's event loop thread")

    async def nested() -> Any:
        async with in_terminal():
            return await app.run_async()

    return asyncio.run_coroutine_threadsafe(nested(), host_loop).result()


if __name__ == "__main__":
    result = pick_model()
    print(result)

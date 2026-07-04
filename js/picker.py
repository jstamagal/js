"""Textual provider/model picker used inside the js REPL.

The CLI login flow owns credentials and model discovery. This picker only
chooses from saved provider logins and their cached model lists.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Header, Label, ListItem, ListView, Static

from . import codex_auth, logins, providers


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
    model_ids = list(cached)
    if provider_id == codex_auth.CODEX_PROVIDER_ID and codex_auth.CODEX_PHANTOM_MODEL_ID not in model_ids:
        model_ids.append(codex_auth.CODEX_PHANTOM_MODEL_ID)
    return [ModelRow(id=model_id, provider=provider_id) for model_id in model_ids]


class ModelPicker(App[dict[str, Any] | None]):
    """Two-pane provider/model picker."""

    CSS = """
    Screen {
        background: #111318;
        color: #d8dee9;
    }
    #body {
        height: 1fr;
        padding: 1 2;
    }
    .pane {
        border: solid #5e81ac;
        padding: 1 2;
        height: 1fr;
    }
    #providers {
        width: 36;
        margin-right: 2;
    }
    #models {
        width: 1fr;
    }
    .title {
        color: #88c0d0;
        text-style: bold;
        margin-bottom: 1;
    }
    #detail {
        height: 3;
        color: #a3be8c;
        margin-top: 1;
    }
    ListView {
        height: 1fr;
    }
    ListView:focus > ListItem.--highlight {
        background: #a3be8c;
        color: #111318;
        text-style: bold;
    }
    Footer {
        background: #1b1f2a;
        color: #a3be8c;
    }
    """

    BINDINGS = [
        ("tab", "toggle_focus", "pane"),
        ("enter", "choose", "select"),
        ("escape", "quit", "quit"),
        ("q", "quit", "quit"),
        ("f", "fetch", "fetch"),
    ]

    def __init__(
        self,
        *,
        provider_id: str | None = None,
        provider_base_url: str | None = None,
        provider_api_key: str | None = None,
        model: str | None = None,
    ) -> None:
        super().__init__()
        self._provider_rows = _provider_rows()
        self._model_rows: list[ModelRow] = []
        self._initial_provider_id = providers.normalize_provider_id(provider_id) if provider_id else None
        self._initial_model = model
        self._override_provider_id = self._initial_provider_id
        self._override_base_url = provider_base_url
        self._override_api_key = provider_api_key

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Horizontal(id="body"):
            with Vertical(id="providers", classes="pane"):
                yield Label("Providers", classes="title")
                yield ListView(id="provider-list")
            with Vertical(id="models", classes="pane"):
                yield Label("Models", classes="title")
                yield ListView(id="model-list")
                yield Static("tab panes • enter select • f fetch • /login adds providers • esc quit", id="detail")
        yield Footer()

    def on_mount(self) -> None:
        provider_list = self.query_one("#provider-list", ListView)
        for row in self._provider_rows:
            provider_list.append(ListItem(Label(f"● {row.id}\n[dim]{row.name}[/dim]")))
        provider_list.focus()
        provider_index = 0
        if self._initial_provider_id:
            for idx, row in enumerate(self._provider_rows):
                if row.id == self._initial_provider_id:
                    provider_index = idx
                    break
        if self._provider_rows:
            provider_list.index = provider_index
            self._load_models(provider_index)
        else:
            self._load_models(0)

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if event.list_view.id == "provider-list":
            self._load_models(event.list_view.index or 0)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.list_view.id == "provider-list":
            if self._model_rows:
                self.query_one("#model-list", ListView).focus()
            return
        if event.list_view.id == "model-list":
            self.action_choose()

    def _load_models(self, provider_index: int) -> None:
        models = self.query_one("#model-list", ListView)
        models.clear()
        if not self._provider_rows:
            self._model_rows = []
            models.append(ListItem(Label("no logged-in providers — use /login or js --login")))
            self.query_one("#detail", Static).update("no logged-in providers — use /login or js --login")
            return
        provider = self._provider_rows[provider_index]
        self._model_rows = _model_rows(provider.id)
        if not self._model_rows:
            models.append(ListItem(Label("no cached models — press f to fetch")))
            self.query_one("#detail", Static).update(f"{provider.id}: no cached models — press f to fetch")
            return
        for row in self._model_rows:
            models.append(ListItem(Label(row.id)))
        selected = 0
        if self._initial_model:
            for idx, row in enumerate(self._model_rows):
                if row.id == self._initial_model:
                    selected = idx
                    break
        models.index = selected
        self.query_one("#detail", Static).update(f"{provider.id} [{provider.source}] — {len(self._model_rows)} model(s)")

    def _current_provider(self) -> ProviderRow | None:
        provider_list = self.query_one("#provider-list", ListView)
        if not self._provider_rows:
            return None
        return self._provider_rows[provider_list.index or 0]

    def _provider_selection_values(self, provider: ProviderRow) -> tuple[str | None, str | None]:
        if provider.id == self._override_provider_id:
            base_url = self._override_base_url if self._override_base_url is not None else provider.provider_base_url
            api_key = self._override_api_key if self._override_api_key is not None else provider.provider_api_key
            return base_url, api_key
        return provider.provider_base_url, provider.provider_api_key

    def action_toggle_focus(self) -> None:
        providers_view = self.query_one("#provider-list", ListView)
        models_view = self.query_one("#model-list", ListView)
        (models_view if providers_view.has_focus else providers_view).focus()

    async def action_fetch(self) -> None:
        provider = self._current_provider()
        if provider is None:
            return
        login = logins.load_logins().get(provider.id)
        if login is None:
            self.query_one("#detail", Static).update(f"{provider.id}: not logged in — use /login first")
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
            idx = self.query_one("#provider-list", ListView).index or 0
            self._load_models(idx)
        except Exception as exc:  # noqa: BLE001
            self.query_one("#detail", Static).update(f"fetch failed: {type(exc).__name__}: {exc}")

    def action_choose(self) -> None:
        provider = self._current_provider()
        if provider is None or not self._model_rows:
            return
        models_view = self.query_one("#model-list", ListView)
        model = self._model_rows[models_view.index or 0]
        base_url, api_key = self._provider_selection_values(provider)
        self.exit(
            {
                "provider_id": provider.id,
                "provider_base_url": base_url,
                "provider_api_key": api_key,
                "provider_headers": dict(provider.provider_headers or {}),
                "model": model.id,
            }
        )


def pick_model(
    *,
    provider_id: str | None = None,
    provider_base_url: str | None = None,
    provider_api_key: str | None = None,
    model: str | None = None,
) -> dict[str, Any] | None:
    """Run the interactive picker and return the selection, or None on cancel."""
    app = ModelPicker(
        provider_id=provider_id,
        provider_base_url=provider_base_url,
        provider_api_key=provider_api_key,
        model=model,
    )
    return app.run()


if __name__ == "__main__":
    result = pick_model()
    print(result)

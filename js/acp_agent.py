"""ACP v1 stdio entry point and session adapter."""

from __future__ import annotations

import asyncio
import fcntl
import json
import mimetypes
import os
import re
import signal
import sys
from base64 import b64decode
from contextlib import ExitStack, contextmanager, redirect_stdout, suppress
from dataclasses import dataclass
from hashlib import sha256
from importlib.metadata import version
from pathlib import Path
from typing import Any
from uuid import uuid4

from acp import RequestError, run_agent, schema as acp
from acp.stdio import stdio_streams

from . import command_line, headless, logins, paths, settings
from .acp_turn import TurnInput
from .config import run_settings
from .mcp_config import MCPConfigError, parse_servers
from .toolkit.reads import atomic_write

TOOL_PREVIEW_CHARS = 8_192
MESSAGE_LIMIT = 4 * 1024 * 1024
MODEL_CONFIG_ID = "model"
MODEL_VALUE = "configured"
TOOL_KINDS = {
    "read": "read", "write": "edit", "edit": "edit", "shell": "execute",
    "patch": "edit", "notebook_edit": "edit", "undo": "edit", "remove": "delete",
    "pi_read": "read", "pi_write": "edit", "pi_edit": "edit", "pi_bash": "execute",
    "search": "search", "fs_search": "search", "fetch": "fetch",
}


@dataclass
class Session:
    cwd: Path
    mcp_servers: dict
    turn: asyncio.Task | None = None
    cancelled: bool = False
    updates: TurnUpdates | None = None
    steers: TurnInput | None = None
    # Between a cancel and what follows it: the prompt request the cancel
    # answered is let go through ``released``, and ``stop`` ends the turn
    # unless a prompt steers it first.
    released: asyncio.Event | None = None
    stop: asyncio.TimerHandle | None = None


def _servers(definitions: list) -> dict:
    servers = {}
    for server in definitions:
        if server.name in servers:
            raise RequestError.invalid_params({"message": f"Duplicate MCP server {server.name!r}"})
        if isinstance(server, acp.McpServerStdio):
            definition = {
                "command": server.command,
                "args": server.args,
                "env": {var.name: var.value for var in server.env},
            }
        elif isinstance(server, acp.HttpMcpServer):
            definition = {
                "url": server.url,
                "headers": {header.name: header.value for header in server.headers},
            }
        else:
            raise RequestError.invalid_params({"message": f"Unsupported MCP transport: {server.type}"})
        servers[server.name] = definition
    try:
        parse_servers(servers)
    except MCPConfigError as exc:
        raise RequestError.invalid_params({"message": str(exc)}) from exc
    return servers


def _prompt_parts(blocks: list, marker: Path) -> tuple[str, list[str]]:
    """The text of a prompt and the files it attaches. An image is written
    under the session's ``marker`` directory, where the worker reads it."""
    parts, files = [], []
    for block in blocks:
        if isinstance(block, acp.TextContentBlock):
            parts.append(block.text)
        elif isinstance(block, acp.ImageContentBlock):
            try:
                data = b64decode(block.data, validate=True)
            except ValueError as exc:
                raise RequestError.invalid_params({"message": "Image data is not base64"}) from exc
            suffix = mimetypes.guess_extension(block.mime_type or "") or ".png"
            path = marker / "attachments" / f"{uuid4().hex}{suffix}"
            path.parent.mkdir(exist_ok=True)
            atomic_write(path, data)
            files.append(str(path))
        elif isinstance(block, acp.EmbeddedResourceContentBlock) and isinstance(
            block.resource, acp.TextResourceContents
        ):
            parts.append(f"Resource {block.resource.uri}\n{block.resource.text}")
        elif isinstance(block, acp.ResourceContentBlock):
            parts.append(f"Resource {block.title or block.name} ({block.uri})")
        else:
            raise RequestError.invalid_params({"message": f"Unsupported prompt content: {block.type}"})
    if not files and not any(part.strip() for part in parts):
        raise RequestError.invalid_params({"message": "Prompt must contain text, an image or a resource reference"})
    return "\n\n".join(parts), files


def _cwd(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or not path.is_dir():
        raise RequestError.invalid_params({"message": "cwd must be an existing absolute directory"})
    return path.resolve()


def _session_marker(cwd: Path, session_id: str) -> Path:
    namespace = sha256(os.fsencode(cwd)).hexdigest()
    return paths.sessions_root() / "acp" / namespace / session_id


def _saved_model(marker: Path) -> str:
    try:
        return (marker / "model").read_text(encoding="utf-8")
    except FileNotFoundError:
        return MODEL_VALUE


@contextmanager
def _session_lock(marker: Path):
    fd = os.open(marker, os.O_RDONLY | os.O_DIRECTORY)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RequestError.invalid_request(
                {"message": "A prompt or model change is already running for this session"}
            ) from exc
        yield
    finally:
        os.close(fd)


def _preview(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= TOOL_PREVIEW_CHARS else text[:TOOL_PREVIEW_CHARS] + "…"


class TurnUpdates:
    def __init__(self, connection, session_id: str, cwd: Path):
        self.connection = connection
        self.session_id = session_id
        self.cwd = cwd
        self.pending: dict[Any, str] = {}
        self.stop_reason = "end_turn"
        self.streamed_text = False
        # Updates kept back while no prompt request is open, None when one is.
        self.held: list | None = None

    async def send(self, update) -> None:
        if self.held is not None:
            self.held.append(update)
            return
        await self.connection.session_update(session_id=self.session_id, update=update)

    async def resume(self) -> None:
        """Send what was kept back, in order, and send from here on."""
        held, self.held = self.held or [], None
        for update in held:
            await self.send(update)

    async def finish_tool(self, call_id: str, status: str, text: str) -> None:
        await self.send(acp.ToolCallProgress(
            tool_call_id=call_id, status=status,
            content=[acp.ContentToolCallContent(content=acp.TextContentBlock(text=text))],
        ))

    async def finish_pending(self, message: str) -> None:
        for call_id in self.pending.values():
            with suppress(ConnectionError, OSError):
                await self.finish_tool(call_id, "failed", message)
        self.pending.clear()

    def tool_fields(self, call: dict) -> dict:
        name = call.get("name") or "Tool"
        kind = TOOL_KINDS.get(name, "other")
        arguments = headless._arguments(call.get("arguments"))
        locations = None
        if kind in {"read", "edit", "delete"} and isinstance(arguments, dict):
            path = arguments.get("file_path") or arguments.get("path")
            if isinstance(path, str) and path and "\0" not in path:
                target = self.cwd / os.path.expanduser(path)
                try:
                    if name == "remove":
                        target = target.parent.resolve() / target.name
                    else:
                        target = target.resolve()
                except (OSError, RuntimeError):
                    pass
                locations = [acp.ToolCallLocation(path=str(target))]
        return dict(title=name, name=name, kind=kind, raw_input=arguments, locations=locations)

    async def start_tool(self, call: dict) -> str:
        call_id = uuid4().hex
        self.pending[call.get("id")] = call_id
        await self.send(acp.ToolCallStart(
            tool_call_id=call_id, status="pending", **self.tool_fields(call),
        ))
        return call_id

    async def approve(self, call: dict) -> str | None:
        call_id = self.pending.get(call.get("id")) or await self.start_tool(call)
        fields = self.tool_fields(call)
        try:
            response = await self.connection.request_permission(
                session_id=self.session_id,
                tool_call=acp.ToolCallUpdate(
                    tool_call_id=call_id, status="pending", **fields,
                ),
                options=[
                    acp.PermissionOption(option_id="allow", name="Allow once", kind="allow_once"),
                    acp.PermissionOption(option_id="deny", name="Reject once", kind="reject_once"),
                ],
            )
        except Exception:
            return "Tool permission could not be obtained from the ACP client"
        outcome = getattr(response, "outcome", None)
        if not isinstance(outcome, acp.AllowedOutcome) or outcome.option_id != "allow":
            return "Tool permission was denied by the ACP client"
        await self.send(acp.ToolCallProgress(tool_call_id=call_id, status="in_progress", **fields))
        return None

    async def emit(self, event: dict) -> None:
        kind = event["type"]
        if kind in {"text", "reasoning"} and (delta := event.get("delta")):
            self.streamed_text |= kind == "text"
            chunk = acp.AgentMessageChunk if kind == "text" else acp.AgentThoughtChunk
            await self.send(chunk(content=acp.TextContentBlock(text=delta)))
        elif kind == "tool_call" and event.get("id") not in self.pending:
            await self.start_tool(event)
        elif kind == "tool_result" and (call_id := self.pending.get(event.get("id"))):
            result = event.get("result")
            if not isinstance(result, str):
                result = event.get("summary") or _preview(result)
            status = "completed" if event.get("ok", True) else "failed"
            await self.finish_tool(call_id, status, _preview(result))
            del self.pending[event.get("id")]
        elif kind == "end":
            if event.get("ok") and not self.streamed_text and (answer := event.get("answer")):
                await self.send(acp.AgentMessageChunk(content=acp.TextContentBlock(text=answer)))
            reason = event.get("stop_reason")
            if reason in {"end_turn", "max_tokens", "max_turn_requests", "refusal", "cancelled"}:
                self.stop_reason = reason


class Agent:
    def __init__(self, options: list[str]):
        self.options = options
        self.sessions: dict[str, Session] = {}
        args = command_line.parser().parse_args(options)
        self.pinned_model = args.model
        self.isolate = True if args.isolate else False if args.no_isolate else None

    def _model_options(self, cwd: Path, current: str) -> list[acp.SessionConfigOptionSelect]:
        if self.pinned_model is not None:
            choices = {self.pinned_model: self.pinned_model}
            current = self.pinned_model
        else:
            choices = {MODEL_VALUE: "js default (session or configured model)"}
            previous = paths.isolate_override()
            paths.configure_isolate(self.isolate)
            try:
                if paths.uses_home(cwd):
                    cache = logins.load_model_cache()
                    for model in sorted({f"{provider}/{name}" for provider, names in cache.items()
                                         if provider for name in names if name}):
                        choices[model] = model
            finally:
                paths.configure_isolate(previous)
            choices.setdefault(current, current)
        return [acp.SessionConfigOptionSelect(
            id=MODEL_CONFIG_ID, name="Model", category="model", current_value=current,
            options=[acp.SessionConfigSelectOption(value=value, name=name)
                     for value, name in choices.items()],
        )]

    def on_connect(self, connection) -> None:
        self.connection = connection

    async def initialize(self, protocol_version: int, **kwargs: Any) -> acp.InitializeResponse:
        return acp.InitializeResponse(
            protocol_version=1,
            agent_info=acp.Implementation(name="js", version=version("js")),
            agent_capabilities=acp.AgentCapabilities(
                mcp_capabilities=acp.McpCapabilities(http=True),
                session_capabilities=acp.SessionCapabilities(resume=acp.SessionResumeCapabilities()),
                prompt_capabilities=acp.PromptCapabilities(embedded_context=True, image=True),
            ),
        )

    async def new_session(
        self, cwd: str, mcp_servers: list | None = None, **kwargs: Any,
    ) -> acp.NewSessionResponse:
        path = _cwd(cwd)
        servers = _servers(mcp_servers or [])
        session_id = uuid4().hex
        try:
            _session_marker(path, session_id).mkdir(parents=True)
        except OSError as exc:
            raise RequestError.internal_error({"message": f"Could not save ACP session: {exc}"}) from exc
        self.sessions[session_id] = Session(path, servers)
        return acp.NewSessionResponse(session_id=session_id, config_options=self._model_options(path, MODEL_VALUE))

    async def resume_session(
        self, session_id: str, cwd: str, mcp_servers: list | None = None, **kwargs: Any,
    ) -> acp.ResumeSessionResponse:
        path = _cwd(cwd)
        if not re.fullmatch(r"[0-9a-f]{12}4[0-9a-f]{3}[89ab][0-9a-f]{15}", session_id):
            raise RequestError.invalid_params({"message": "Invalid ACP session ID"})
        if not _session_marker(path, session_id).is_dir():
            raise RequestError.resource_not_found(session_id)
        session = self.sessions.get(session_id)
        if session and session.turn is not None:
            raise RequestError.invalid_request({"message": "A prompt is already running for this session"})
        self.sessions[session_id] = Session(path, _servers(mcp_servers or []))
        return acp.ResumeSessionResponse(config_options=self._model_options(
            path, _saved_model(_session_marker(path, session_id)),
        ))

    async def set_config_option(
        self, config_id: str, session_id: str, value: str | bool, **kwargs: Any,
    ) -> acp.SetSessionConfigOptionResponse:
        session = self.sessions.get(session_id)
        if session is None or config_id != MODEL_CONFIG_ID or not isinstance(value, str):
            raise RequestError.invalid_params({"message": "Unknown session, config option or value"})
        marker = _session_marker(session.cwd, session_id)
        with _session_lock(marker):
            options = self._model_options(session.cwd, _saved_model(marker))
            if not any(option.value == value for option in options[0].options):
                raise RequestError.invalid_params({"message": "Unknown model"})
            if self.pinned_model is None:
                atomic_write(marker / "model", value.encode("utf-8"))
                options[0].current_value = value
        return acp.SetSessionConfigOptionResponse(config_options=options)

    async def prompt(self, session_id: str, prompt: list, **kwargs: Any) -> acp.PromptResponse:
        session = self.sessions.get(session_id)
        if session is None:
            raise RequestError.invalid_params({"message": "Unknown session"})
        marker = _session_marker(session.cwd, session_id)
        text, files = _prompt_parts(prompt, marker)
        if session.turn is not None and not session.turn.done():
            if session.stop is None:
                raise RequestError.invalid_request({"message": "A prompt is already running for this session"})
            # A cancel and then a prompt is the client steering the turn: the
            # turn goes on and takes the prompt at its next tool boundary.
            session.stop.cancel()
            session.stop = None
            await session.steers.steer(text, files)
            await session.updates.resume()
        else:
            if session.stop is not None:
                # The turn that cancel was for has ended: its timer is not this turn's.
                session.stop.cancel()
                session.stop = None
            if session.updates is not None:
                # What the last turn said after its prompt request was answered.
                await session.updates.resume()
            held = ExitStack()
            held.enter_context(_session_lock(marker))
            session.cancelled = False
            session.updates = TurnUpdates(self.connection, session_id, session.cwd)
            session.steers = TurnInput()
            session.turn = asyncio.create_task(self._turns(session, session_id, text, files, marker))

            def ended(turn: asyncio.Task) -> None:
                held.close()
                if not turn.cancelled():
                    turn.exception()  # a turn no request is waiting on still ends observed

            session.turn.add_done_callback(ended)
        return await self._follow(session)

    async def _turns(self, session: Session, session_id: str, text: str, files: list[str],
                     marker: Path) -> None:
        """The turn of ``text`` and ``files``, then one more for each steer no worker took:
        it came after the last tool boundary."""
        from .acp_turn import run_turn

        updates, steers = session.updates, session.steers
        options = self.options
        if self.pinned_model is None and (model := _saved_model(marker)) != MODEL_VALUE:
            options = [*options, f"--model={model}"]
        try:
            while True:
                await run_turn(
                    cwd=session.cwd, session_id=session_id, text=text, options=options,
                    mcp_servers=session.mcp_servers, emit=updates.emit, approve=updates.approve,
                    steers=steers, files=files,
                )
                if not steers.waiting:
                    return
                text, files = steers.take_waiting()
        except asyncio.CancelledError:
            updates.held = None
            with suppress(Exception):
                await updates.finish_pending("Cancelled")
            raise

    async def _follow(self, session: Session) -> acp.PromptResponse:
        """The response of a prompt request to the session's running turn."""
        turn, updates = session.turn, session.updates
        session.released = released = asyncio.Event()
        let_go = asyncio.create_task(released.wait())
        try:
            await asyncio.wait({turn, let_go}, return_when=asyncio.FIRST_COMPLETED)
            if not turn.done():
                return acp.PromptResponse(stop_reason="cancelled")
            await turn
            await updates.finish_pending("Turn ended before the tool returned")
            return acp.PromptResponse(stop_reason=updates.stop_reason)
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                if not turn.cancelling():
                    turn.cancel()
                with suppress(asyncio.CancelledError, ConnectionError):
                    await turn
                raise
            if not session.cancelled:
                raise
            return acp.PromptResponse(stop_reason="cancelled")
        except Exception as exc:
            await updates.finish_pending("Turn failed")
            raise RequestError(-32603, _preview(str(exc)) or "js turn failed") from exc
        finally:
            let_go.cancel()
            if session.turn is turn and turn.done():
                session.turn = None

    def _stop(self, session: Session, turn: asyncio.Task | None = None) -> None:
        """End ``turn``, the turn a cancel was for (None: the session's turn),
        if it is still the session's and still running."""
        session.stop = None
        turn = turn or session.turn
        if turn is not None and turn is session.turn and not turn.done():
            session.cancelled = True
            turn.cancel()

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        """End the session's turn, unless a prompt comes within ACP_STEER_WAIT
        seconds: the protocol has no steer, so a client that steers sends a
        cancel and then the prompt. The open prompt request is answered now."""
        session = self.sessions.get(session_id)
        if (session is None or session.turn is None or session.turn.done()
                or session.cancelled or session.stop is not None):
            return
        wait = int(settings.knob(run_settings(session.cwd), "ACP_STEER_WAIT"))
        if wait <= 0:
            self._stop(session)
            return
        session.updates.held = []
        session.stop = asyncio.get_running_loop().call_later(wait, self._stop, session, session.turn)
        if session.released is not None:
            session.released.set()


async def _serve(options: list[str]) -> int:
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    terminating = False
    agent = Agent(options)

    def terminate() -> None:
        nonlocal terminating
        if not terminating:
            terminating = True
            for session in agent.sessions.values():
                if session.turn and not session.turn.cancelling():
                    session.turn.cancel()
            task.cancel()

    if os.name == "posix":
        loop.add_signal_handler(signal.SIGTERM, terminate)
    try:
        reader, writer = await stdio_streams(limit=MESSAGE_LIMIT)
        with redirect_stdout(sys.stderr):
            await run_agent(agent, writer, reader, use_unstable_protocol=True)
    except asyncio.CancelledError:
        if not terminating:
            raise
    finally:
        if os.name == "posix":
            loop.remove_signal_handler(signal.SIGTERM)
    return 128 + signal.SIGTERM if terminating else 0


def run(options: list[str]) -> int:
    """Serve ACP on stdio until the client disconnects."""
    try:
        return asyncio.run(_serve(options))
    except KeyboardInterrupt:
        return 130

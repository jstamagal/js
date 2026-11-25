"""ACP v1 stdio entry point and session adapter."""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import re
import signal
import sys
from contextlib import contextmanager, redirect_stdout, suppress
from dataclasses import dataclass
from hashlib import sha256
from importlib.metadata import version
from pathlib import Path
from typing import Any
from uuid import uuid4

from acp import RequestError, run_agent, schema as acp
from acp.stdio import stdio_streams

from . import command_line, headless, logins, paths
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


def _prompt_text(blocks: list) -> str:
    parts = []
    for block in blocks:
        if isinstance(block, acp.TextContentBlock):
            parts.append(block.text)
        elif isinstance(block, acp.EmbeddedResourceContentBlock) and isinstance(
            block.resource, acp.TextResourceContents
        ):
            parts.append(f"Resource {block.resource.uri}\n{block.resource.text}")
        elif isinstance(block, acp.ResourceContentBlock):
            parts.append(f"Resource {block.title or block.name} ({block.uri})")
        else:
            raise RequestError.invalid_params({"message": f"Unsupported prompt content: {block.type}"})
    if not any(part.strip() for part in parts):
        raise RequestError.invalid_params({"message": "Prompt must contain text or a resource reference"})
    return "\n\n".join(parts)


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

    async def send(self, update) -> None:
        await self.connection.session_update(session_id=self.session_id, update=update)

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
                prompt_capabilities=acp.PromptCapabilities(embedded_context=True),
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
        if session.turn is not None:
            raise RequestError.invalid_request({"message": "A prompt is already running for this session"})
        text = _prompt_text(prompt)
        marker = _session_marker(session.cwd, session_id)
        with _session_lock(marker):
            options = self.options
            if self.pinned_model is None and (model := _saved_model(marker)) != MODEL_VALUE:
                options = [*options, f"--model={model}"]
            return await self._prompt(session, session_id, text, options)

    async def _prompt(
        self, session: Session, session_id: str, text: str, options: list[str],
    ) -> acp.PromptResponse:
        from .acp_turn import run_turn

        updates = TurnUpdates(self.connection, session_id, session.cwd)
        session.cancelled = False
        session.turn = asyncio.create_task(run_turn(
            cwd=session.cwd, session_id=session_id, text=text, options=options,
            mcp_servers=session.mcp_servers, emit=updates.emit, approve=updates.approve,
        ))
        try:
            await asyncio.shield(session.turn)
            await updates.finish_pending("Turn ended before the tool returned")
            return acp.PromptResponse(stop_reason=updates.stop_reason)
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                if not session.turn.cancelling():
                    session.turn.cancel()
                with suppress(asyncio.CancelledError, ConnectionError):
                    await session.turn
                raise
            if not session.cancelled:
                raise
            await updates.finish_pending("Cancelled")
            return acp.PromptResponse(stop_reason="cancelled")
        except Exception as exc:
            await updates.finish_pending("Turn failed")
            raise RequestError(-32603, _preview(str(exc)) or "js turn failed") from exc
        finally:
            session.turn = None

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        session = self.sessions.get(session_id)
        if session and session.turn and not session.turn.done() and not session.cancelled:
            session.cancelled = True
            session.turn.cancel()


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

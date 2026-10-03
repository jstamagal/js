"""`lsp`: diagnostics, definition, references and hover from a language server.

The `lsp.servers` setting lists the servers: each entry names a command, the
file extensions it serves and the files that mark a workspace root. For a
file, the first entry that serves its extension and whose command is on PATH
is used.

One server process runs per (entry, workspace root, jail) for the life of the
js process; later calls reuse it. Each call sends the file's current content
from disk (didOpen, then didChange and didSave after it changed), so a call
after an edit sees the edit. Under `js -C` the server runs in the jail.
"""

from __future__ import annotations

import atexit
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlparse

from .. import jail as _jail
from .. import paths
from .core import Tool, ToolContext
from .descriptions import load_description
from .sanitize import int_or_default

OPERATIONS = ("diagnostics", "definition", "references", "hover")

# After the first diagnostics for a new version arrive, later publishes within
# this many seconds replace them: some servers publish in stages.
_DIAGNOSTIC_SETTLE_S = 0.3
# Most locations a definition or references result lists.
_MAX_LOCATIONS = 200
_STDERR_LINES = 20
# How long a fresh server gets to send its first `experimental/serverStatus`.
_STATUS_PROBE_S = 1.0
_SEVERITY = {1: "error", 2: "warning", 3: "info", 4: "hint"}
_LANGUAGE_IDS = {
    ".py": "python", ".pyi": "python",
    ".rs": "rust",
    ".go": "go",
    ".ts": "typescript", ".mts": "typescript", ".cts": "typescript",
    ".tsx": "typescriptreact",
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".jsx": "javascriptreact",
}


class LspError(Exception):
    """A language server failed a call. The message is one line or a short tail."""


@dataclass(frozen=True)
class ServerSpec:
    name: str
    command: tuple[str, ...]
    extensions: tuple[str, ...]
    roots: tuple[str, ...] = ()


def parse_servers(raw: Any) -> list[ServerSpec]:
    """The `lsp.servers` setting as server specs. Raises LspError naming the
    first entry that is not {name, command, extensions[, roots]}."""
    if not isinstance(raw, list):
        raise LspError("lsp.servers must be a JSON list of {name, command, extensions, roots} objects")
    specs: list[ServerSpec] = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise LspError(f"lsp.servers entry {index} is not an object")
        command = entry.get("command")
        extensions = entry.get("extensions")
        roots = entry.get("roots", [])
        if (not isinstance(command, list) or not command
                or not all(isinstance(part, str) and part for part in command)):
            raise LspError(f"lsp.servers entry {index}: command must be a non-empty list of strings")
        if not isinstance(extensions, list) or not all(isinstance(ext, str) for ext in extensions):
            raise LspError(f"lsp.servers entry {index}: extensions must be a list of strings such as \".py\"")
        if not isinstance(roots, list) or not all(isinstance(marker, str) for marker in roots):
            raise LspError(f"lsp.servers entry {index}: roots must be a list of file names")
        name = entry.get("name")
        specs.append(ServerSpec(
            name=str(name) if isinstance(name, str) and name else command[0],
            command=tuple(command),
            extensions=tuple(ext.lower() if ext.startswith(".") else f".{ext.lower()}" for ext in extensions),
            roots=tuple(roots),
        ))
    return specs


def pick_server(path: Path, specs: list[ServerSpec], env_path: str | None = None) -> tuple[ServerSpec, str]:
    """The first spec serving ``path``'s extension whose command is on PATH,
    with the command's resolved path. Raises LspError when there is none."""
    ext = path.suffix.lower()
    serving = [spec for spec in specs if ext and ext in spec.extensions]
    if not serving:
        covered = sorted({ext for spec in specs for ext in spec.extensions})
        kind = f"{ext} files" if ext else "files without an extension"
        listing = ", ".join(covered) if covered else "nothing"
        raise LspError(f"no language server is configured for {kind}; lsp.servers covers {listing}")
    for spec in serving:
        found = shutil.which(spec.command[0], path=env_path)
        if found:
            return spec, found
    looked = ", ".join(dict.fromkeys(spec.command[0] for spec in serving))
    raise LspError(
        f"no language server for {ext} files is on PATH (looked for {looked}); "
        "install one or add an entry to lsp.servers"
    )


def workspace_root(path: Path, spec: ServerSpec, context: ToolContext) -> Path:
    """The directory the server treats as its workspace: the nearest ancestor
    holding one of the spec's root markers, else the nearest holding `.git`,
    else the working directory when it holds the file, else the file's
    directory. The operator's home and `/` are never a root, and under `js -C`
    neither is a directory outside the jail."""
    jail = _jail.active()
    never = {Path("/"), paths.user_home().resolve()}
    for markers in (spec.roots, (".git",)):
        if not markers:
            continue
        for directory in path.parents:
            if jail is not None and not jail.bound(directory, context.jail_bind):
                break
            if directory in never:
                continue
            if any((directory / marker).exists() for marker in markers):
                return directory
    cwd = Path(context.cwd).resolve()
    if cwd in path.parents and cwd not in never:
        return cwd
    return path.parent


def file_uri(path: Path) -> str:
    return "file://" + quote(str(path), safe="/")


def uri_path(uri: str) -> Path | None:
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        return None
    return Path(unquote(parsed.path))


def split_lines(text: str) -> list[str]:
    """``text``'s lines as a language server counts them: split at \\n, \\r\\n and \\r only."""
    return re.split(r"\r\n|\r|\n", text)


def utf16_column(line: str, column: int) -> int:
    """UTF-16 code units before code point ``column`` (0-based) of ``line``."""
    return len(line[:column].encode("utf-16-le")) // 2


def codepoint_column(line: str, units: int) -> int:
    """The 0-based code point column at UTF-16 offset ``units`` of ``line``."""
    count = 0
    for index, char in enumerate(line):
        if count >= units:
            return index
        count += 2 if ord(char) > 0xFFFF else 1
    return len(line)


@dataclass
class _Document:
    version: int
    text: str


@dataclass
class _Diagnostics:
    seq: int
    version: int | None
    items: list[dict]


@dataclass
class LanguageServer:
    """One running language server and the JSON-RPC conversation with it."""

    spec: ServerSpec
    root: Path
    process: subprocess.Popen
    documents: dict[str, _Document] = field(default_factory=dict)
    capabilities: dict = field(default_factory=dict)
    stderr_tail: deque = field(default_factory=lambda: deque(maxlen=_STDERR_LINES))
    exited: bool = False
    _next_id: int = 0
    _responses: dict[int, dict] = field(default_factory=dict)
    _diagnostics: dict[str, _Diagnostics] = field(default_factory=dict)
    _publishes: int = 0
    # `experimental/serverStatus` (rust-analyzer): None until the server sends
    # one, then whether it has finished loading the workspace.
    _quiescent: bool | None = None
    _status_probed: bool = False
    _cond: threading.Condition = field(default_factory=threading.Condition)
    _write_lock: threading.Lock = field(default_factory=threading.Lock)
    # Held while documents are compared with disk and synced, so two parallel
    # calls never send the same version twice.
    sync_lock: threading.Lock = field(default_factory=threading.Lock)

    def start_threads(self) -> None:
        threading.Thread(target=self._read_loop, name=f"lsp-{self.spec.name}", daemon=True).start()
        threading.Thread(target=self._stderr_loop, name=f"lsp-{self.spec.name}-stderr", daemon=True).start()

    @property
    def alive(self) -> bool:
        return not self.exited and self.process.poll() is None

    # --- wire ---------------------------------------------------------------

    def _send(self, message: dict) -> None:
        body = json.dumps({"jsonrpc": "2.0", **message}, ensure_ascii=False).encode("utf-8")
        frame = b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\n\r\n" + body
        with self._write_lock:
            stdin = self.process.stdin
            if stdin is None:
                raise LspError(self._exit_detail())
            try:
                stdin.write(frame)
                stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as exc:
                raise LspError(self._exit_detail()) from exc

    def _read_message(self) -> dict | None:
        stdout = self.process.stdout
        assert stdout is not None
        length = None
        while True:
            line = stdout.readline()
            if not line:
                return None
            text = line.decode("ascii", errors="replace").strip()
            if not text:
                if length is not None:
                    break
                continue
            name, _, value = text.partition(":")
            if name.strip().lower() == "content-length":
                length = int(value.strip())
        body = stdout.read(length)
        if len(body) < length:
            return None
        return json.loads(body.decode("utf-8"))

    def _read_loop(self) -> None:
        try:
            while True:
                try:
                    message = self._read_message()
                except (ValueError, json.JSONDecodeError):
                    continue
                if message is None:
                    break
                self._dispatch(message)
        except (OSError, ValueError):
            pass
        finally:
            with self._cond:
                self.exited = True
                self._cond.notify_all()

    def _stderr_loop(self) -> None:
        stderr = self.process.stderr
        if stderr is None:
            return
        try:
            for line in stderr:
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    self.stderr_tail.append(text)
        except (OSError, ValueError):
            pass

    def _dispatch(self, message: dict) -> None:
        method = message.get("method")
        if method is None:
            if "id" in message:
                with self._cond:
                    self._responses[message["id"]] = message
                    self._cond.notify_all()
            return
        if "id" in message:
            self._answer(message["id"], method, message.get("params"))
            return
        if method == "experimental/serverStatus":
            with self._cond:
                self._quiescent = bool((message.get("params") or {}).get("quiescent"))
                self._cond.notify_all()
            return
        if method == "textDocument/publishDiagnostics":
            params = message.get("params") or {}
            uri = params.get("uri")
            if isinstance(uri, str):
                with self._cond:
                    self._publishes += 1
                    version = params.get("version")
                    self._diagnostics[uri] = _Diagnostics(
                        self._publishes, version if isinstance(version, int) else None,
                        list(params.get("diagnostics") or []),
                    )
                    self._cond.notify_all()

    def _answer(self, request_id: Any, method: str, params: Any) -> None:
        """Reply to a request the server sent. js offers no settings, so a
        configuration request gets null for every item."""
        result: Any = None
        if method == "workspace/configuration":
            items = params.get("items") if isinstance(params, dict) else None
            result = [None] * (len(items) if isinstance(items, list) else 0)
        elif method == "workspace/workspaceFolders":
            result = [{"uri": file_uri(self.root), "name": self.root.name}]
        elif method not in {"client/registerCapability", "client/unregisterCapability",
                            "window/workDoneProgress/create", "window/showMessageRequest",
                            "workspace/diagnostic/refresh", "workspace/semanticTokens/refresh",
                            "workspace/inlayHint/refresh", "workspace/codeLens/refresh"}:
            try:
                self._send({"id": request_id, "error": {"code": -32601, "message": f"js does not handle {method}"}})
            except LspError:
                pass
            return
        try:
            self._send({"id": request_id, "result": result})
        except LspError:
            pass

    def _exit_detail(self) -> str:
        code = self.process.poll()
        head = f"the server exited (code {code})" if code is not None else "the server closed its pipes"
        tail = " | ".join(self.stderr_tail)
        return f"{head}; stderr: {tail}" if tail else head

    def request(self, method: str, params: Any, timeout: float) -> Any:
        with self._cond:
            self._next_id += 1
            request_id = self._next_id
        self._send({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        with self._cond:
            while request_id not in self._responses:
                if self.exited:
                    raise LspError(f"{method}: {self._exit_detail()}")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cond.wait(remaining)
            response = self._responses.pop(request_id, None)
        if response is None:
            try:
                self.notify("$/cancelRequest", {"id": request_id})
            except LspError:
                pass
            raise LspError(f"no reply to {method} within {timeout:g}s (lsp.timeout_s)")
        error = response.get("error")
        if isinstance(error, dict):
            raise LspError(f"{method} failed: {error.get('message', error)}")
        return response.get("result")

    def notify(self, method: str, params: Any) -> None:
        self._send({"method": method, "params": params})

    # --- lifecycle ----------------------------------------------------------

    def initialize(self, timeout: float) -> None:
        root_uri = file_uri(self.root)
        capabilities = {
            "general": {"positionEncodings": ["utf-16"]},
            "workspace": {"configuration": True, "workspaceFolders": True},
            "textDocument": {
                "synchronization": {"didSave": True, "dynamicRegistration": False},
                "publishDiagnostics": {"relatedInformation": False, "versionSupport": True},
                "hover": {"contentFormat": ["markdown", "plaintext"]},
                "definition": {"linkSupport": True},
                "references": {},
            },
            "window": {"workDoneProgress": False},
            "experimental": {"serverStatusNotification": True},
        }
        result = self.request("initialize", {
            "processId": os.getpid(),
            "clientInfo": {"name": "js"},
            "rootUri": root_uri,
            "rootPath": str(self.root),
            "workspaceFolders": [{"uri": root_uri, "name": self.root.name}],
            "capabilities": capabilities,
        }, timeout)
        self.capabilities = (result or {}).get("capabilities") or {}
        self.notify("initialized", {})

    def shutdown(self) -> None:
        if self.alive:
            try:
                self.request("shutdown", None, 2)
                self.notify("exit", None)
            except LspError:
                pass
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except OSError:
                self.process.kill()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass

    # --- documents ----------------------------------------------------------

    def _saves(self) -> tuple[bool, bool]:
        """(send didSave, include the text in it), from the server's capabilities."""
        sync = self.capabilities.get("textDocumentSync")
        if not isinstance(sync, dict):
            return False, False
        save = sync.get("save")
        if isinstance(save, dict):
            return True, bool(save.get("includeText"))
        return bool(save), False

    def sync(self, path: Path, text: str) -> tuple[int, bool]:
        """Send ``text`` as ``path``'s content. Returns (version, changed).
        The caller holds ``sync_lock``."""
        uri = file_uri(path)
        document = self.documents.get(uri)
        if document is None:
            self.documents[uri] = _Document(1, text)
            self.notify("textDocument/didOpen", {"textDocument": {
                "uri": uri, "languageId": _LANGUAGE_IDS.get(path.suffix.lower(), self.spec.name),
                "version": 1, "text": text,
            }})
            return 1, True
        if document.text == text:
            return document.version, False
        document.version += 1
        document.text = text
        self.notify("textDocument/didChange", {
            "textDocument": {"uri": uri, "version": document.version},
            "contentChanges": [{"text": text}],
        })
        save, include_text = self._saves()
        if save:
            params: dict[str, Any] = {"textDocument": {"uri": uri}}
            if include_text:
                params["text"] = text
            self.notify("textDocument/didSave", params)
        return document.version, True

    def refresh(self, skip: str, read: Any) -> None:
        """Resync every open document but ``skip`` from disk: ``read(path)``
        returns its text, or None when it is gone. The caller holds ``sync_lock``."""
        for uri in list(self.documents):
            if uri == skip:
                continue
            path = uri_path(uri)
            text = read(path) if path is not None else None
            if text is None:
                del self.documents[uri]
                self.notify("textDocument/didClose", {"textDocument": {"uri": uri}})
                continue
            self.sync(path, text)

    def publishes(self, uri: str) -> int:
        with self._cond:
            entry = self._diagnostics.get(uri)
            return entry.seq if entry is not None else 0

    def cached_diagnostics(self, uri: str, version: int) -> list[dict] | None:
        with self._cond:
            entry = self._diagnostics.get(uri)
            if entry is None or (entry.version is not None and entry.version != version):
                return None
            return list(entry.items)

    def wait_quiescent(self, timeout: float) -> None:
        """Return once a server that reports `experimental/serverStatus` has
        loaded its workspace; before that it answers position queries with
        nothing. The first call gives the server a moment to send its first
        status; a server that sends none is never waited on again."""
        deadline = time.monotonic() + timeout
        with self._cond:
            if self._quiescent is None and not self._status_probed:
                self._status_probed = True
                self._cond.wait_for(lambda: self._quiescent is not None or self.exited,
                                    min(_STATUS_PROBE_S, timeout))
            while self._quiescent is False and not self.exited:
                left = deadline - time.monotonic()
                if left <= 0:
                    return
                self._cond.wait(left)

    def wait_diagnostics(self, uri: str, version: int, after: int, timeout: float) -> list[dict] | None:
        """The diagnostics published for ``uri`` after publish number
        ``after`` (and for ``version`` when the server says which version they
        are for), once none newer arrive for a moment. None when none arrive
        within ``timeout``."""
        deadline = time.monotonic() + timeout

        def fresh() -> _Diagnostics | None:
            entry = self._diagnostics.get(uri)
            if entry is None or entry.seq <= after:
                return None
            if entry.version is not None and entry.version < version:
                return None
            return entry

        with self._cond:
            entry = fresh()
            while entry is None:
                if self.exited:
                    raise LspError(self._exit_detail())
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cond.wait(remaining)
                entry = fresh()
            seen, quiet_from = entry.seq, time.monotonic()
            while True:
                remaining = min(deadline, quiet_from + _DIAGNOSTIC_SETTLE_S) - time.monotonic()
                if remaining <= 0 or self.exited:
                    break
                self._cond.wait(remaining)
                latest = fresh()
                if latest is not None and latest.seq != seen:
                    entry, seen, quiet_from = latest, latest.seq, time.monotonic()
            return list(entry.items)


_SERVERS: dict[tuple, LanguageServer] = {}
_SERVERS_LOCK = threading.Lock()


@atexit.register
def shutdown_servers() -> None:
    """Stop every language server this process started."""
    with _SERVERS_LOCK:
        servers = list(_SERVERS.values())
        _SERVERS.clear()
    for server in servers:
        server.shutdown()


def _server_env(context: ToolContext) -> dict[str, str]:
    """The environment a server starts with: js's own, or under `js -C` only
    the names `limits.shell_env_allow` lists."""
    if _jail.active() is None:
        return dict(os.environ)
    allow = tuple(getattr(context, "shell_env_allow", ()) or ())
    return {key: os.environ[key] for key in allow if key in os.environ}


def server_for(spec: ServerSpec, executable: str, root: Path, context: ToolContext,
               timeout: float) -> LanguageServer:
    """The running server for ``spec`` at ``root``, started and initialized
    when there is none. Raises LspError when it cannot start."""
    jail = _jail.active()
    key = (spec, str(root), None if jail is None else str(jail.root),
           tuple(context.jail_bind) if jail is not None else ())
    with _SERVERS_LOCK:
        server = _SERVERS.get(key)
        if server is not None and server.alive:
            return server
        if server is not None:
            del _SERVERS[key]
            server.shutdown()
        env = _server_env(context)
        # In the jail the command's directory is bound back, and so is every
        # symlink on the way to the file it resolves to.
        command = Path(executable)
        argv = _jail.wrap([executable, *spec.command[1:]], context, cwd=root, env=env,
                          extra_ro=(command.parent, command))
        try:
            process = subprocess.Popen(
                argv, cwd=root, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, start_new_session=True,
            )
        except OSError as exc:
            raise LspError(f"could not start {executable}: {exc}") from exc
        server = LanguageServer(spec=spec, root=root, process=process)
        server.start_threads()
        try:
            server.initialize(timeout)
        except LspError as exc:
            server.shutdown()
            hint = " Under -C it runs in the jail; jail.bind adds paths it needs." if jail is not None else ""
            raise LspError(f"did not start: {exc}.{hint}") from exc
        _SERVERS[key] = server
        return server


def _read_text(path: Path, context: ToolContext) -> str | None:
    try:
        target = context.resolve_path(path)
        data = target.read_bytes()
    except (OSError, _jail.JailError):
        return None
    if len(data) > context.max_file_bytes:
        return None
    return data.decode("utf-8", errors="replace")


def _display(path: Path, context: ToolContext) -> str:
    cwd = Path(context.cwd).resolve()
    try:
        return str(path.relative_to(cwd))
    except ValueError:
        return str(path)


def _position(lines: list[str], line: Any, character: Any, symbol: Any) -> tuple[dict, str] | str:
    """The LSP position for a 1-based line and either a 1-based character or
    the first occurrence of ``symbol`` on that line, with a label for it.
    Returns an ERROR line when there is none."""
    number = int_or_default(line, 0, minimum=1)
    if number == 0:
        return "ERROR: line is required for this operation: a 1-based line number"
    if number > len(lines):
        return f"ERROR: line {number} is past the end of the file ({len(lines)} lines)"
    text = lines[number - 1]
    column = int_or_default(character, 0, minimum=1)
    if column:
        if column > len(text) + 1:
            return f"ERROR: character {column} is past the end of line {number} ({len(text)} characters)"
        index = column - 1
    elif isinstance(symbol, str) and symbol:
        match = re.search(rf"(?<!\w){re.escape(symbol)}(?!\w)", text)
        index = match.start() if match else text.find(symbol)
        if index < 0:
            return f"ERROR: {symbol!r} is not on line {number}: {text.strip()}"
    else:
        return "ERROR: pass character (1-based column) or symbol (text on the line) with line"
    return {"line": number - 1, "character": utf16_column(text, index)}, f"{number}:{index + 1}"


def _locations(result: Any) -> list[tuple[str, dict]]:
    """(uri, start position) of each Location or LocationLink in ``result``."""
    items = result if isinstance(result, list) else [result] if isinstance(result, dict) else []
    out: list[tuple[str, dict]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if "targetUri" in item:
            span = item.get("targetSelectionRange") or item.get("targetRange") or {}
            out.append((item["targetUri"], span.get("start") or {}))
        elif "uri" in item:
            out.append((item["uri"], (item.get("range") or {}).get("start") or {}))
    return out


def _format_locations(result: Any, context: ToolContext, label: str) -> str:
    locations = _locations(result)
    if not locations:
        return f"no {label} found"
    cache: dict[str, list[str] | None] = {}
    rows: list[str] = []
    for uri, start in locations[:_MAX_LOCATIONS]:
        path = uri_path(uri)
        line = int(start.get("line", 0)) if isinstance(start.get("line"), int) else 0
        units = int(start.get("character", 0)) if isinstance(start.get("character"), int) else 0
        if path is None:
            rows.append(f"{uri}:{line + 1}:{units + 1}")
            continue
        if uri not in cache:
            text = _read_text(path, context)
            cache[uri] = split_lines(text) if text is not None else None
        lines = cache[uri]
        source = lines[line] if lines is not None and line < len(lines) else None
        column = codepoint_column(source, units) + 1 if source is not None else units + 1
        row = f"{_display(path, context)}:{line + 1}:{column}"
        rows.append(f"{row}: {source.strip()}" if source is not None and source.strip() else row)
    more = len(locations) - _MAX_LOCATIONS
    head = f"{len(locations)} {label}" if len(locations) != 1 else f"1 {label.rstrip('s')}"
    tail = f"\n[+{more} more not shown]" if more > 0 else ""
    return head + ":\n" + "\n".join(rows) + tail


def _hover_text(contents: Any) -> str:
    if isinstance(contents, str):
        return contents
    if isinstance(contents, dict):
        value = str(contents.get("value", ""))
        language = contents.get("language")
        return f"```{language}\n{value}\n```" if language else value
    if isinstance(contents, list):
        return "\n\n".join(part for part in (_hover_text(item) for item in contents) if part)
    return ""


def _format_diagnostics(items: list[dict], lines: list[str], display: str, server: str) -> str:
    if not items:
        return f"{display}: no diagnostics ({server})"
    rows: list[tuple[int, int, str]] = []
    for item in items:
        start = ((item.get("range") or {}).get("start")) or {}
        line = start.get("line", 0) if isinstance(start.get("line"), int) else 0
        units = start.get("character", 0) if isinstance(start.get("character"), int) else 0
        column = codepoint_column(lines[line], units) if line < len(lines) else units
        severity = _SEVERITY.get(item.get("severity"), "error")
        message = str(item.get("message", "")).strip().replace("\n", "\n    ")
        origin = " ".join(str(part) for part in (item.get("source"), item.get("code")) if part not in (None, ""))
        suffix = f" [{origin}]" if origin else ""
        rows.append((line, column, f"{line + 1}:{column + 1}: {severity}: {message}{suffix}"))
    rows.sort(key=lambda row: (row[0], row[1]))
    noun = "diagnostic" if len(rows) == 1 else "diagnostics"
    return f"{display}: {len(rows)} {noun} ({server})\n" + "\n".join(row[2] for row in rows)


def lsp(
    operation: str,
    file_path: str,
    line: int | None = None,
    character: int | None = None,
    symbol: str | None = None,
    context: ToolContext | None = None,
) -> str:
    assert context is not None
    op = str(operation or "").strip().lower()
    if op not in OPERATIONS:
        return f"ERROR: operation must be one of {', '.join(OPERATIONS)}"
    if not file_path:
        return "ERROR: file_path is required"
    target = context.resolve_path(file_path)
    if not target.is_file():
        return f"ERROR: no such file: {target}"
    try:
        size = target.stat().st_size
        if size > context.max_file_bytes:
            return (f"ERROR: file size ({size} bytes) exceeds limits.max_file_bytes "
                    f"({context.max_file_bytes}) for a language server")
        text = target.read_bytes().decode("utf-8")
    except OSError as exc:
        return f"ERROR: {exc}"
    except UnicodeDecodeError:
        return f"ERROR: {target} is not UTF-8 text"
    env_path = _server_env(context).get("PATH")
    try:
        spec, executable = pick_server(target, parse_servers(context.lsp_servers), env_path)
    except LspError as exc:
        return f"ERROR: {exc}"
    timeout = float(int_or_default(context.lsp_timeout_s, 30, minimum=1))
    root = workspace_root(target, spec, context)
    lines = split_lines(text)
    try:
        server = server_for(spec, executable, root, context, timeout)
        uri = file_uri(target)
        with server.sync_lock:
            server.refresh(uri, lambda path: _read_text(path, context))
            before = server.publishes(uri)
            version, changed = server.sync(target, text)
        display = _display(target, context)
        if op == "diagnostics":
            items = None if changed else server.cached_diagnostics(uri, version)
            if items is None:
                items = server.wait_diagnostics(uri, version, before, timeout)
            if items is None:
                return (f"{display}: {spec.name} published no diagnostics within {timeout:g}s "
                        "(lsp.timeout_s); it may still be loading the workspace. Call again.")
            return _format_diagnostics(items, lines, display, spec.name)
        position = _position(lines, line, character, symbol)
        if isinstance(position, str):
            return position
        where, label = position
        document = {"textDocument": {"uri": uri}, "position": where}
        server.wait_quiescent(timeout)
        if op == "hover":
            result = server.request("textDocument/hover", document, timeout)
            body = _hover_text((result or {}).get("contents")) if isinstance(result, dict) else ""
            return body.strip() or f"no hover information at {display}:{label}"
        if op == "definition":
            result = server.request("textDocument/definition", document, timeout)
            return _format_locations(result, context, "definitions")
        result = server.request("textDocument/references",
                                {**document, "context": {"includeDeclaration": True}}, timeout)
        return _format_locations(result, context, "references")
    except LspError as exc:
        return f"ERROR: {spec.name}: {exc}"


def tools() -> tuple[Tool, ...]:
    return (
        Tool(
            "lsp",
            load_description("lsp"),
            lsp,
            {
                "operation": {"type": "string", "enum": list(OPERATIONS),
                              "description": "diagnostics for the file, or definition, references or hover at a position."},
                "file_path": {"type": "string", "description": "Absolute, relative, or ~ path to a source file."},
                "line": {"type": "integer", "description": "1-based line of the position. Not used by diagnostics."},
                "character": {"type": "integer", "description": "1-based column on that line."},
                "symbol": {"type": "string", "description": "Text on that line to put the position on, instead of character."},
            },
            required=("operation", "file_path"),
            read_only=True,
        ),
    )

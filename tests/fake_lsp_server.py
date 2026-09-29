"""A small language server over stdio for the lsp tool's tests.

It serves any file. A line holding ERROR gets an error diagnostic at that
word. Definition finds `def NAME` in the open documents, references finds
every NAME, and hover names the word at the position. Hovering `whoami`
returns the entries of $HOME as the server sees them. Positions are UTF-16,
as LSP counts them.

Usage: fake_lsp_server.py LOG [--crash] [--no-diagnostics]
LOG gets one JSON line per message the client sent.
"""

from __future__ import annotations

import json
import os
import re
import sys

LOG = sys.argv[1]
FLAGS = set(sys.argv[2:])
documents: dict[str, str] = {}
stdin = sys.stdin.buffer
stdout = sys.stdout.buffer
next_id = 1000


def log(message: dict) -> None:
    with open(LOG, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(message) + "\n")


def send(message: dict) -> None:
    body = json.dumps({"jsonrpc": "2.0", **message}).encode("utf-8")
    stdout.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
    stdout.flush()


def receive() -> dict | None:
    length = None
    while True:
        line = stdin.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            break
        name, _, value = line.decode("ascii").partition(":")
        if name.lower() == "content-length":
            length = int(value)
    return json.loads(stdin.read(length))


def lines_of(uri: str) -> list[str]:
    return re.split(r"\r\n|\r|\n", documents.get(uri, ""))


def units(line: str, index: int) -> int:
    return len(line[:index].encode("utf-16-le")) // 2


def index_of(line: str, utf16: int) -> int:
    count = 0
    for index, char in enumerate(line):
        if count >= utf16:
            return index
        count += 2 if ord(char) > 0xFFFF else 1
    return len(line)


def word_at(uri: str, position: dict) -> str:
    line = lines_of(uri)[position["line"]]
    index = index_of(line, position["character"])
    for match in re.finditer(r"\w+", line):
        if match.start() <= index < match.end():
            return match.group()
    return ""


def location(uri: str, number: int, line: str, start: int, word: str) -> dict:
    return {"uri": uri, "range": {
        "start": {"line": number, "character": units(line, start)},
        "end": {"line": number, "character": units(line, start + len(word))},
    }}


def publish(uri: str, version: int) -> None:
    if "--no-diagnostics" in FLAGS:
        return
    diagnostics = []
    for number, line in enumerate(lines_of(uri)):
        column = line.find("ERROR")
        if column >= 0:
            diagnostics.append({
                "range": {"start": {"line": number, "character": units(line, column)},
                          "end": {"line": number, "character": units(line, column + 5)}},
                "severity": 1, "source": "fake", "message": "found ERROR",
            })
    send({"method": "textDocument/publishDiagnostics",
          "params": {"uri": uri, "version": version, "diagnostics": diagnostics}})


def handle(message: dict) -> None:
    global next_id
    method = message.get("method")
    params = message.get("params") or {}
    if method is None:
        return
    if method == "initialize":
        send({"id": message["id"], "result": {"capabilities": {
            "textDocumentSync": {"openClose": True, "change": 1, "save": {"includeText": False}},
            "hoverProvider": True, "definitionProvider": True, "referencesProvider": True,
        }}})
    elif method == "initialized":
        next_id += 1
        send({"id": next_id, "method": "workspace/configuration",
              "params": {"items": [{"section": "fake"}]}})
    elif method == "textDocument/didOpen":
        document = params["textDocument"]
        documents[document["uri"]] = document["text"]
        publish(document["uri"], document["version"])
    elif method == "textDocument/didChange":
        document = params["textDocument"]
        documents[document["uri"]] = params["contentChanges"][-1]["text"]
        publish(document["uri"], document["version"])
    elif method == "textDocument/didClose":
        documents.pop(params["textDocument"]["uri"], None)
    elif method == "textDocument/hover":
        word = word_at(params["textDocument"]["uri"], params["position"])
        if word == "whoami":
            value = json.dumps(sorted(os.listdir(os.path.expanduser("~"))))
        else:
            value = f"hover `{word}`" if word else ""
        send({"id": message["id"], "result": {"contents": {"kind": "markdown", "value": value}} if value else None})
    elif method in ("textDocument/definition", "textDocument/references"):
        word = word_at(params["textDocument"]["uri"], params["position"])
        found = []
        pattern = rf"def\s+({word})\b" if method.endswith("definition") else rf"\b({word})\b"
        for uri in documents:
            for number, line in enumerate(lines_of(uri)):
                for match in re.finditer(pattern, line):
                    found.append(location(uri, number, line, match.start(1), word))
        send({"id": message["id"], "result": found})
    elif method == "shutdown":
        send({"id": message["id"], "result": None})
    elif method == "exit":
        sys.exit(0)
    elif "id" in message:
        send({"id": message["id"], "error": {"code": -32601, "message": method}})


def main() -> None:
    if "--crash" in FLAGS:
        sys.stderr.write("fake server failed to boot\n")
        sys.stderr.flush()
        sys.exit(3)
    while True:
        message = receive()
        if message is None:
            return
        log(message)
        handle(message)


if __name__ == "__main__":
    main()

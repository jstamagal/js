"""One turn through `runtime.run_turn` with a scripted model.

The model makes each batch of tool calls in turn, one batch per model call,
then answers. A test reads what came out: the history, what each model call
received, and the turn's flight log.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path

import ai
import ai.types.messages
import ai.types.usage

from js import output, runtime
from js.config import Config
from js.console import ConsoleSink
from js.model_client import ModelStreamResult, ModelToolCall
from js.toolkit.core import Tool, ToolContext
from js.toolkit.registry import ToolRegistry

# One tool call the model makes: (call id, tool name, arguments text).
Call = tuple[str, str, str]


def reply(*calls: Call, text: str = "") -> ModelStreamResult:
    """A model reply making ``calls``, or answering ``text`` when there are none."""
    tool_calls = [ModelToolCall(id=call_id, name=name, arguments=args) for call_id, name, args in calls]
    parts: list = [
        ai.types.messages.ToolCallPart(tool_call_id=call.id, tool_name=call.name, tool_args=call.arguments)
        for call in tool_calls
    ]
    if text or not parts:
        parts.append(ai.types.messages.TextPart(text=text))
    return ModelStreamResult(
        text=text,
        tool_calls=tool_calls,
        reasoning="",
        usage=ai.types.usage.Usage(input_tokens=100, output_tokens=1),
        finish_reason="tool_calls" if tool_calls else "stop",
        assistant_message=ai.messages.Message(role="assistant", parts=parts),
    )


def config(tmp_path: Path, settings: dict | None = None, **fields) -> Config:
    """A Config for an offline turn; ``fields`` replace its fields."""
    sessions = tmp_path / ".js" / "sessions" / "turn-test"
    cfg = Config(
        agent_id="turn-test",
        agent_dir=sessions,
        model="offline-test",
        provider_id=None,
        provider_base_url=None,
        provider_api_key=None,
        reasoning_effort=None,
        max_output_tokens=None,
        max_tool_iterations=8,
        max_bash_output_bytes=65536,
        max_tool_result_bytes=65536,
        fetch_timeout_s=5,
        debug_log=None,
        trace=False,
        history_file=tmp_path / ".history",
        sessions_dir=sessions,
        session_file=sessions / "session.jsonl",
        prompts_dir=tmp_path / "prompts",
        settings=settings or {},
    )
    return replace(cfg, **fields) if fields else cfg


def registry(*tools: Tool) -> ToolRegistry:
    """A registry of exactly ``tools``."""
    return ToolRegistry(tools=tools, aliases={tool.name: tool.name for tool in tools})


def recording(name: str, params: dict | None = None, required: tuple[str, ...] = (),
              result: str = "ok") -> tuple[Tool, list[dict]]:
    """A tool that returns ``result`` and the list of the arguments each of its
    calls received."""
    received: list[dict] = []

    def handler(**kwargs):
        received.append(kwargs)
        return result

    return Tool(name, f"{name} tool", handler, params or {}, required=required), received


class ExchangeConsole:
    """An output sink that draws the turn's tool exchanges on the console and
    nothing else."""

    EVENTS = frozenset({"tool_call", "tool_result", "tools_settle"})

    def __init__(self) -> None:
        self.console = ConsoleSink(None)

    def emit(self, event: output.OutputEvent) -> None:
        if event.name in self.EVENTS:
            self.console.emit(event)


@dataclass
class Turn:
    """What a scripted turn left behind."""

    messages: list[dict]
    # The messages each model call received, in order.
    received: list[list]
    # The names of the tools each model call offered, in order.
    offered: list[list[str]]
    flight: Path

    def calls(self) -> list[tuple[str, str, str]]:
        """Every tool call the history records, as (id, name, arguments text)."""
        return [(call["id"], call["function"]["name"], call["function"]["arguments"])
                for message in self.messages for call in message.get("tool_calls") or ()]

    def results(self) -> dict[str, str]:
        """The result the history records for each tool call id."""
        return {message["tool_call_id"]: message["content"]
                for message in self.messages if message.get("role") == "tool"}

    def received_results(self) -> list[dict[str, object]]:
        """Per batch, the results the next model call received, by call id."""
        return [
            {part.tool_call_id: part.result
             for message in convo for part in message.parts if part.kind == "tool_result"}
            for convo in self.received[1:]
        ]

    def replayed_calls(self) -> list[tuple[str, str, str]]:
        """The tool calls the last model call received in its history, as
        (id, name, arguments text)."""
        return [(part.tool_call_id, part.tool_name, part.tool_args)
                for message in self.received[-1] for part in message.parts if part.kind == "tool_call"]

    def events(self, kind: str) -> list[dict]:
        """The flight log records of ``kind``."""
        if not self.flight.exists():
            return []
        records = [json.loads(line) for line in self.flight.read_text(encoding="utf-8").splitlines()]
        return [record for record in records if record["kind"] == kind]


def script(tmp_path: Path, monkeypatch, batches: list[list[Call]], *, answer: str = "done") -> Turn:
    """Make the model's calls ``batches``, then ``answer``; returns the Turn the
    caller runs on its ``messages``."""
    turn = Turn([{"role": "user", "content": "go"}], [], [], tmp_path / "flight.jsonl")

    async def model(**kwargs):
        turn.received.append(list(kwargs["messages"]))
        turn.offered.append([tool.name for tool in kwargs.get("tools") or ()])
        index = len(turn.received) - 1
        return reply(*batches[index]) if index < len(batches) else reply(text=answer)

    monkeypatch.setattr(runtime.model_client, "stream_model_async", model)
    return turn


def _turn_arguments(tmp_path: Path, turn: Turn, tool_registry: ToolRegistry, *, cfg: Config | None,
                    context: ToolContext | None, sink, trace: bool, suppress_output: bool,
                    options: dict) -> tuple[tuple, dict]:
    """The arguments of `runtime.run_turn` for ``turn``."""
    args = (cfg or config(tmp_path), "SYS", turn.messages, runtime.Telemetry(turn.flight, sink=sink))
    return args, dict(trace_override=trace, tool_registry=tool_registry,
                      tool_context=context or ToolContext(cwd=tmp_path), suppress_output=suppress_output,
                      **options)


def run(tmp_path: Path, monkeypatch, tool_registry: ToolRegistry, batches: list[list[Call]], *,
        cfg: Config | None = None, context: ToolContext | None = None, sink=None,
        trace: bool = False, suppress_output: bool = True, **options) -> Turn:
    """Run one turn whose model makes ``batches`` of calls, then answers.
    ``options`` are further keyword arguments of `runtime.run_turn`."""
    turn = script(tmp_path, monkeypatch, batches)
    args, kwargs = _turn_arguments(tmp_path, turn, tool_registry, cfg=cfg, context=context, sink=sink,
                                   trace=trace, suppress_output=suppress_output, options=options)
    runtime.run_turn(*args, **kwargs)
    return turn


async def run_async(tmp_path: Path, monkeypatch, tool_registry: ToolRegistry, batches: list[list[Call]], *,
                    cfg: Config | None = None, context: ToolContext | None = None, sink=None,
                    trace: bool = False, suppress_output: bool = True, **options) -> Turn:
    """`run` on the running event loop, through `runtime.run_turn_async`."""
    turn = script(tmp_path, monkeypatch, batches)
    args, kwargs = _turn_arguments(tmp_path, turn, tool_registry, cfg=cfg, context=context, sink=sink,
                                   trace=trace, suppress_output=suppress_output, options=options)
    await runtime.run_turn_async(*args, **kwargs)
    return turn

"""An assistant record survives the trip to an SDK message and back.

`history_to_ai_messages` turns a record into the message a request sends;
`assistant_record` writes the record of a message. Given the message a record
became, it writes that record again: the decoder puts every signed part, text
block and tool call where the encoder found them. The records are those the
reply cases of test_assistant_record_replay wrote.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from js import history_record

RECORDS = Path(__file__).parent / "fixtures" / "assistant_record_replay"


def _records() -> list[tuple[str, dict]]:
    return [(path.stem, record) for path in sorted(RECORDS.glob("*.json"))
            for record in json.loads(path.read_text())["records"]]


def _origin(record: dict) -> tuple[str | None, str | None]:
    origin = record.get("reasoning_from") or {}
    return origin.get("provider"), origin.get("model")


@pytest.mark.parametrize(("name", "record"), _records(), ids=[name for name, _ in _records()])
def test_an_assistant_record_comes_back_from_the_message_it_becomes(name, record):
    provider_id, model_id = _origin(record)
    [message] = history_record.history_to_ai_messages("", [record], provider_id=provider_id, model_id=model_id)

    again, _message = history_record.assistant_record(
        message,
        text=record["content"],
        tool_calls=[(call["id"], call["function"]["name"], call["function"]["arguments"])
                    for call in record.get("tool_calls", [])],
        reasoning_content=record.get("reasoning_content", ""),
        reasoning_blocks=record.get("reasoning_blocks"),
        provider_metadata=record.get("provider_metadata"),
        incomplete_reason=record.get("incomplete_reason"),
        provider_id=provider_id,
        model_id=model_id,
    )

    assert again == record


def test_the_records_cover_signed_reasoning_and_its_layout():
    records = [record for _name, record in _records()]
    assert any(record.get("reasoning_layout") for record in records)
    assert any(call.get("text_before") for record in records for call in record.get("tool_calls", []))
    assert any(record.get("reasoning_blocks") for record in records)

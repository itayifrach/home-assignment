"""Offline tests for decoding the `answer` field out of a partially generated JSON object."""
import json

import pytest

from app.agent.loop import AnswerFieldStreamer


def _stream(payload: str, chunk: int) -> str:
    s, out = AnswerFieldStreamer(), ""
    for i in range(0, len(payload), chunk):
        out += s.feed(payload[i:i + chunk])
    return out


@pytest.mark.parametrize("chunk", [1, 2, 3, 7, 50])
def test_reassembles_answer_with_escapes_at_any_chunking(chunk):
    answer = 'Dallas is **High** — "severe storms" lead.\nLine 2 \\ tab\there, 98.1°F, emoji ⛈️'
    payload = json.dumps({"answer": answer, "question_type": "explanation", "hubs": []}, ensure_ascii=True)
    assert _stream(payload, chunk) == answer


def test_ignores_text_before_answer_key_and_stops_at_closing_quote():
    payload = '{"answer": "short", "caveats": ["not part of the answer"]}'
    assert _stream(payload, 4) == "short"


def test_no_answer_key_yields_nothing():
    assert _stream('{"other": "x"}', 3) == ""

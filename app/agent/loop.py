"""The agent loop: Claude picks tools, our code executes them, Claude returns schema-validated JSON.

`ask_stream` is the single implementation: a generator of progress events (used by the streaming
endpoint). `ask` simply drains it and returns the final response (used by the JSON endpoint and evals).
"""
import json
import re
import time
import uuid
from collections.abc import Iterator

import anthropic
from pydantic import ValidationError

from app.agent.prompts import system_prompt
from app.agent.schemas import AgentAnswer, ChatResponse, strict_json_schema
from app.agent.tools import TOOLS, run_tool
from app.config import ANTHROPIC_EFFORT, ANTHROPIC_MODEL, MAX_AGENT_STEPS
from app.scoring.scoring import get_scores
from app.storage import db

ANSWER_SCHEMA = strict_json_schema(AgentAnswer)
_client: anthropic.Anthropic | None = None


def client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


def _stream_claude(messages: list[dict]) -> Iterator[str | object]:
    """Yield text deltas as they arrive, then the final accumulated message as the last item."""
    with client().beta.messages.stream(
        model=ANTHROPIC_MODEL,
        max_tokens=16000,
        system=system_prompt(),
        tools=TOOLS,
        messages=messages,
        output_config={"effort": ANTHROPIC_EFFORT,
                       "format": {"type": "json_schema", "schema": ANSWER_SCHEMA}},
        cache_control={"type": "ephemeral"},  # tools + system + history are a stable prefix
        # If a safety classifier declines, retry server-side on Anthropic's recommended fallback model.
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    ) as stream:
        for event in stream:
            if event.type == "text":
                yield event.text
        yield stream.get_final_message()


class AnswerFieldStreamer:
    """Incrementally decode the `answer` string field out of a JSON object that is still being generated.

    `answer` is the first property in the schema, so it streams first; we show it live while the rest
    of the JSON (drivers, caveats...) is still being written.
    """
    _START = re.compile(r'"answer"\s*:\s*"')

    def __init__(self):
        self.buf, self.emitted = "", ""

    def feed(self, delta: str) -> str:
        self.buf += delta
        m = self._START.search(self.buf)
        if not m:
            return ""
        raw, i, s = [], m.end(), self.buf
        while i < len(s):
            ch = s[i]
            if ch == '"':
                break                                   # end of the string value
            if ch == "\\":
                esc_len = 6 if s[i + 1:i + 2] == "u" else 2
                if i + esc_len > len(s):
                    break                               # incomplete escape: wait for more input
                raw.append(s[i:i + esc_len])
                i += esc_len
                continue
            raw.append(ch)
            i += 1
        try:
            decoded = json.loads('"' + "".join(raw) + '"')
        except json.JSONDecodeError:
            return ""
        new = decoded[len(self.emitted):]
        self.emitted = decoded
        return new


def _verify_numbers(answer: AgentAnswer) -> list[str]:
    """Guardrail: every hub/score the LLM reports is checked against the deterministic model."""
    warnings = []
    hubs = get_scores()["hubs"]
    for m in answer.hubs:
        h = hubs.get(m.hub_id)
        if not h:
            warnings.append(f"Answer mentions unknown hub '{m.hub_id}'.")
            continue
        if m.score is None:
            continue
        truth = h["overall_score"] if m.score_type == "overall" else h["hazards"].get(m.score_type, {}).get("score")
        if truth is None:
            warnings.append(f"{m.hub_id}: unrecognized score_type '{m.score_type}'.")
        elif abs(truth - m.score) > 0.5:
            warnings.append(f"{m.hub_id} {m.score_type} score reported as {m.score}, model says {truth}; "
                            "corrected.")
            m.score = truth
    return warnings


def ask_stream(question: str, session_id: str | None = None) -> Iterator[dict]:
    """Run one chat turn, yielding events:
    status / tool / answer_delta / answer_reset, and finally {"type": "final", "response": ChatResponse}.
    """
    session_id = session_id or uuid.uuid4().hex
    t0 = time.time()
    messages = db.load_session(session_id)
    messages.append({"role": "user", "content": question})
    trace: list[dict] = []
    usage = {"llm_calls": 0, "input_tokens": 0, "cache_read_input_tokens": 0,
             "cache_creation_input_tokens": 0, "output_tokens": 0}
    answer: AgentAnswer | None = None
    error = None
    model_used = ANTHROPIC_MODEL

    try:
        for step in range(MAX_AGENT_STEPS + 1):
            yield {"type": "status", "message": "Thinking…" if step == 0 else "Reviewing tool results…"}
            t_llm = time.time()
            streamer, resp = AnswerFieldStreamer(), None
            for item in _stream_claude(messages):
                if isinstance(item, str):
                    if delta := streamer.feed(item):
                        yield {"type": "answer_delta", "text": delta}
                else:
                    resp = item
            model_used = resp.model
            usage["llm_calls"] += 1
            for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens"):
                usage[k] += getattr(resp.usage, k, 0) or 0
            trace.append({"step": step, "llm_call": True, "ms": int(1000 * (time.time() - t_llm)),
                          "stop_reason": resp.stop_reason})
            # Append the full content unchanged (thinking blocks included) - history is append-only.
            messages.append({"role": "assistant",
                             "content": [b.model_dump(mode="json", exclude_none=True) for b in resp.content]})

            if resp.stop_reason == "tool_use":
                if streamer.emitted:   # rare: text streamed before deciding to call a tool
                    yield {"type": "answer_reset"}
                results = []
                for b in resp.content:
                    if b.type != "tool_use":
                        continue
                    t_tool = time.time()
                    out, is_err = run_tool(b.name, b.input)
                    entry = {"step": step, "tool": b.name, "input": b.input, "is_error": is_err,
                             "ms": int(1000 * (time.time() - t_tool)), "output_preview": out[:300]}
                    trace.append(entry)
                    yield {"type": "tool", **{k: entry[k] for k in ("tool", "input", "is_error", "ms")}}
                    results.append({"type": "tool_result", "tool_use_id": b.id, "content": out,
                                    "is_error": is_err})
                messages.append({"role": "user", "content": results})
                if step == MAX_AGENT_STEPS - 1:
                    messages.append({"role": "user", "content": "Tool budget reached. Give your final answer "
                                                                "now with the data you have."})
                continue

            if resp.stop_reason == "refusal":
                error = "The model declined to answer this request."
                break
            if resp.stop_reason == "max_tokens":
                error = "The answer was cut off (max_tokens)."
                break

            text = "".join(b.text for b in resp.content if b.type == "text")
            try:
                answer = AgentAnswer.model_validate_json(text)
            except ValidationError as e:
                # Structured outputs should prevent this; retry once with the error as feedback.
                if step < MAX_AGENT_STEPS:
                    yield {"type": "answer_reset"}
                    messages.append({"role": "user", "content": f"Your answer failed schema validation: {e}. "
                                                                "Return a corrected JSON answer."})
                    continue
                error = f"Answer failed validation: {e}"
            break
        else:
            error = "Agent did not finish within the step limit."
    except anthropic.APIConnectionError as e:
        error = f"Could not reach the Claude API: {e}"
    except anthropic.AuthenticationError:
        error = "The Claude API key is missing or invalid. Set ANTHROPIC_API_KEY in .env and restart."
    except anthropic.RateLimitError:
        error = "Claude API rate limit hit; please retry in a moment."
    except anthropic.APIStatusError as e:
        error = f"Claude API error {e.status_code}: {e.message}"

    latency_ms = int(1000 * (time.time() - t0))
    if answer is None:
        answer = AgentAnswer(
            answer=f"Sorry, I couldn't complete that request. {error or ''}".strip(),
            question_type="other", hubs=[], key_drivers=[], data_sources=[], assumptions=[],
            caveats=[error or "unknown error"], confidence="low", confidence_reason="No answer was produced.",
            suggested_followups=[],
        )
        warnings = [error or "unknown error"]
        # Don't persist a broken turn: the next question starts from the last good state.
    else:
        warnings = _verify_numbers(answer)
        db.save_session(session_id, messages)

    db.log_chat(session_id, question, answer.model_dump(), trace, latency_ms, error)
    yield {"type": "final", "response": ChatResponse(
        **answer.model_dump(), session_id=session_id, tool_trace=trace,
        validation_warnings=warnings, latency_ms=latency_ms, model=model_used, usage=usage)}


def ask(question: str, session_id: str | None = None) -> ChatResponse:
    """Non-streaming convenience wrapper: run the turn and return the final response."""
    final = None
    for event in ask_stream(question, session_id):
        if event["type"] == "final":
            final = event["response"]
    return final


if __name__ == "__main__":  # quick manual test:  python -m app.agent.loop "your question"
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    db.init_db()
    for ev in ask_stream(" ".join(sys.argv[1:]) or "Which Midwest hubs are most exposed to winter disruption?"):
        if ev["type"] == "answer_delta":
            print(ev["text"], end="", flush=True)
        elif ev["type"] == "final":
            print("\n\n" + json.dumps(ev["response"].model_dump(exclude={"answer"}), indent=2))
        else:
            print(f"[{ev['type']}] {ev.get('message') or ev.get('tool', '')} {ev.get('input', '')}")

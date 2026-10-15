"""The agent loop: Claude picks tools, our code executes them, Claude returns schema-validated JSON."""
import json
import time
import uuid

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


def _call_claude(messages: list[dict]):
    return client().beta.messages.create(
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
    )


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


def ask(question: str, session_id: str | None = None) -> ChatResponse:
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
            t_llm = time.time()
            resp = _call_claude(messages)
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
                results = []
                for b in resp.content:
                    if b.type != "tool_use":
                        continue
                    t_tool = time.time()
                    out, is_err = run_tool(b.name, b.input)
                    trace.append({"step": step, "tool": b.name, "input": b.input, "is_error": is_err,
                                  "ms": int(1000 * (time.time() - t_tool)),
                                  "output_preview": out[:300]})
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
    return ChatResponse(**answer.model_dump(), session_id=session_id, tool_trace=trace,
                        validation_warnings=warnings, latency_ms=latency_ms, model=model_used, usage=usage)


if __name__ == "__main__":  # quick manual test:  python -m app.agent.loop "your question"
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    db.init_db()
    r = ask(" ".join(sys.argv[1:]) or "Which Midwest hubs are most exposed to winter disruption?")
    print(json.dumps(r.model_dump(), indent=2))

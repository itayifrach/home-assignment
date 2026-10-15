"""Run the agent evaluation set.

Usage:
  python -m evals.run_evals                 # all cases, in-process
  python -m evals.run_evals -k denver       # only cases whose id contains "denver"
Results are written to evals/results/<timestamp>.json. Exit code 1 if any case fails.
"""
import argparse
import sys
import json
import re
import time
import uuid
from datetime import datetime
from pathlib import Path

import yaml

from app.agent.loop import ask
from app.agent.tools import run_tool
from app.scoring.scoring import rank_hubs
from app.storage import db

HERE = Path(__file__).parent


def _numbers(text: str) -> list[float]:
    return [float(x.replace(",", "")) for x in re.findall(r"-?\d[\d,]*\.?\d*", text)]


def grade(resp, checks: dict, conversation_tools: set[str] | None = None) -> list[str]:
    """Return a list of failure messages (empty = pass).

    Tool checks look at the whole conversation: in a follow-up the agent may correctly answer from data it
    already fetched in an earlier turn (cheaper and faster), which must not count as a failure.
    """
    fails = []
    tools = conversation_tools if conversation_tools is not None else {t["tool"] for t in resp.tool_trace if "tool" in t}
    hub_ids = [h.hub_id for h in resp.hubs]
    text = resp.answer.lower()

    if "tools_any" in checks and not tools & set(checks["tools_any"]):
        fails.append(f"expected one of tools {checks['tools_any']}, called {sorted(tools) or 'none'}")
    if checks.get("tools_none") and tools:
        fails.append(f"expected no tool calls, called {sorted(tools)}")
    if "question_type" in checks and resp.question_type not in checks["question_type"]:
        fails.append(f"question_type {resp.question_type!r} not in {checks['question_type']}")
    for h in checks.get("hubs_include", []):
        if h not in hub_ids:
            fails.append(f"hub {h!r} missing from answer.hubs {hub_ids}")
    for h in checks.get("hubs_exclude", []):
        if h in hub_ids:
            fails.append(f"hub {h!r} should not appear in answer.hubs")
    if "first_hub_is_top" in checks:
        c = checks["first_hub_is_top"]
        top = rank_hubs(c["hazard"], c.get("region"))[0]["hub_id"]
        if not hub_ids or hub_ids[0] != top:
            fails.append(f"first hub should be deterministic #1 {top!r}, got {hub_ids[:3]}")
    if "answer_contains_any" in checks and not any(s.lower() in text for s in checks["answer_contains_any"]):
        fails.append(f"answer contains none of {checks['answer_contains_any']}")
    if "number_matches" in checks:
        c = checks["number_matches"]
        out, is_err = run_tool(c["tool"], c["args"])
        if is_err:
            fails.append(f"ground-truth tool call failed: {out}")
        else:
            truth = json.loads(out)[c["field"]]
            if not any(abs(n - truth) <= c["tolerance"] for n in _numbers(resp.answer)):
                fails.append(f"expected {c['field']}={truth} (±{c['tolerance']}) in answer")
    if resp.validation_warnings:
        fails.append(f"guardrail warnings: {resp.validation_warnings}")
    return fails


def run_case(case: dict):
    session = f"eval-{case['id']}-{uuid.uuid4().hex[:6]}"
    turns = case.get("turns") or [case["question"]]
    resp, tools = None, set()
    for q in turns:
        resp = ask(q, session)
        tools |= {t["tool"] for t in resp.tool_trace if "tool" in t}
    return resp, tools


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # Windows consoles default to a legacy code page
    ap = argparse.ArgumentParser()
    ap.add_argument("-k", default="", help="only run cases whose id contains this")
    args = ap.parse_args()

    db.init_db()
    cases = [c for c in yaml.safe_load((HERE / "cases.yaml").read_text(encoding="utf-8")) if args.k in c["id"]]
    rows, t_all = [], time.time()
    for c in cases:
        t0 = time.time()
        try:
            resp, conv_tools = run_case(c)
            fails = grade(resp, c["checks"], conv_tools)
            detail = {"answer": resp.answer, "tools": sorted(conv_tools), "usage": resp.usage,
                      "hubs": [h.hub_id for h in resp.hubs], "question_type": resp.question_type}
        except Exception as e:  # a crash is a failure, not an aborted run
            fails, detail = [f"crashed: {type(e).__name__}: {e}"], {}
        secs = time.time() - t0
        rows.append({"id": c["id"], "passed": not fails, "failures": fails, "seconds": round(secs, 1), **detail})
        mark = "PASS" if not fails else "FAIL"
        print(f"{mark}  {c['id']:<36} {secs:5.1f}s  {'; '.join(fails)}", flush=True)

    passed = sum(r["passed"] for r in rows)
    print(f"\n{passed}/{len(rows)} passed in {time.time() - t_all:.0f}s")
    out_dir = HERE / "results"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"{datetime.now():%Y%m%d-%H%M%S}.json"
    out.write_text(json.dumps({"passed": passed, "total": len(rows), "cases": rows}, indent=2), encoding="utf-8")
    print(f"Results: {out}")
    return 0 if passed == len(rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())

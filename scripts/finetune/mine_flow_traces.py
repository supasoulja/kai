#!/usr/bin/env python3
"""
mine_flow_traces.py — turn Kai's recorded flow logs into finetune material.

`kai/core/flow.py` writes one row per step of every turn (route → triage → otto →
tool → answer) to the `flow_log` table. That's your real, in-distribution record of
how the crew actually routed and which tools it called. This script replays it into
two outputs:

  1. otto_routing.jsonl  — TRAINING-READY Otto examples (system=otto.md, user=the
     request, assistant=the DISPATCH/FINISH line). Only the FIRST Otto decision of
     each turn is emitted, because that's the one whose context (empty scratchpad)
     we can reconstruct exactly. Multi-step reconstruction is a TODO.

  2. seeds.jsonl — one row per turn: {input, handoff, profile, specialist, tool_calls}.
     These are SEEDS, not training data: the flow log truncates payloads and does not
     store tool OUTPUTS, so a complete specialist trace can't be rebuilt from it. Feed
     these to the synthetic generator and to tool_test_loop.py, which re-runs the
     tools live to capture real outputs.

Honest scope: the flow log is a debug X-ray (lossy, 6 KB field cap, no outputs). It
gives you gold Otto-routing data and high-signal tool-intent seeds — not finished
multi-turn specialist traces. Those come from the test loop.

Requires that FLOW_TRACE was enabled (kai/config.py) when the turns were recorded.

    python scripts/finetune/mine_flow_traces.py --limit 2000 --out-dir data/mined
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from kai.core import crew, flow


def _steps_by_turn(limit: int) -> list[tuple[str, list[dict]]]:
    """Most-recent `limit` turns as (trace_id, ordered steps)."""
    turns = flow.recent_turns(limit=limit)
    out = []
    for t in turns:
        tid = t["trace_id"]
        steps = flow.get_flow(tid)
        if steps:
            out.append((tid, steps))
    return out


def _first(steps: list[dict], kind: str) -> dict | None:
    return next((s for s in steps if s.get("kind") == kind), None)


def _otto_example(user_input: str, otto_line: str) -> dict:
    """Reconstruct the exact prompt crew_runner._otto_decide builds on step 1
    (empty scratchpad), paired with the DISPATCH/FINISH line the model produced."""
    system = crew.load_specialist_prompt("Otto")
    user = (
        f"User request: {user_input}\n\n"
        "Findings so far:\n(nothing gathered yet)\n\n"
        "Output your one line now (DISPATCH <specialist>: <subtask>  or  FINISH: <summary>):"
    )
    return {
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
            {"role": "assistant", "content": otto_line.strip()},
        ],
        "meta": {"specialist": "Otto", "source": "flow"},
    }


def mine(limit: int, out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    otto_path = out_dir / "otto_routing.jsonl"
    seeds_path = out_dir / "seeds.jsonl"

    stats = Counter()
    seen_otto: set[str] = set()  # dedup identical (input, line) pairs

    with (
        otto_path.open("w", encoding="utf-8") as otto_fh,
        seeds_path.open("w", encoding="utf-8") as seeds_fh,
    ):
        for tid, steps in _steps_by_turn(limit):
            stats["turns"] += 1
            route = _first(steps, "route")
            user_input = (route or {}).get("input", "").strip()
            if not user_input:
                continue

            # ── Otto routing example (first decision only) ──
            otto = _first(steps, "otto")
            if otto:
                line = (otto.get("text") or "").strip()
                # parse_otto_decision tolerates surrounding prose; keep only real lines.
                if line and crew.parse_otto_decision(line):
                    key = f"{user_input}␟{line}"
                    if key not in seen_otto:
                        seen_otto.add(key)
                        otto_fh.write(
                            json.dumps(_otto_example(user_input, line), ensure_ascii=False) + "\n"
                        )
                        stats["otto_examples"] += 1

            # ── Seed row (per turn) ──
            triage = _first(steps, "triage") or {}
            tool_calls = []
            for s in steps:
                if s.get("kind") != "tool":
                    continue
                raw = s.get("args", "")
                try:
                    parsed = json.loads(raw) if isinstance(raw, str) else (raw or {})
                except json.JSONDecodeError:
                    parsed = {}
                tool_calls.append({"name": s.get("name", ""), "args": parsed})

            seeds_fh.write(
                json.dumps(
                    {
                        "trace_id": tid,
                        "input": user_input,
                        "handoff": (route or {}).get("handoff"),
                        "profile": triage.get("profile"),
                        "specialist": triage.get("specialist"),
                        "tool_calls": tool_calls,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            stats["seeds"] += 1
            if tool_calls:
                stats["seeds_with_tools"] += 1

    return {"stats": dict(stats), "otto": str(otto_path), "seeds": str(seeds_path)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--limit", type=int, default=2000, help="most-recent turns to scan")
    ap.add_argument("--out-dir", default="data/mined", help="output directory")
    args = ap.parse_args(argv)

    result = mine(args.limit, Path(args.out_dir))
    s = result["stats"]
    if not s.get("turns"):
        print(
            "No flow turns found. Was FLOW_TRACE enabled when these turns ran?\n"
            "Set FLOW_TRACE = True in kai/config.py, use Kai for a while, then re-run.",
            file=sys.stderr,
        )
        return 1
    print(f"scanned {s.get('turns', 0)} turns")
    print(f"  Otto routing examples : {s.get('otto_examples', 0)}  → {result['otto']}")
    print(
        f"  seeds                 : {s.get('seeds', 0)} "
        f"({s.get('seeds_with_tools', 0)} with tool calls)  → {result['seeds']}"
    )
    print(
        "\nNext: feed seeds.jsonl to tool_test_loop.py to capture real tool outputs,\n"
        "then verify_examples.py to gate the result before training."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

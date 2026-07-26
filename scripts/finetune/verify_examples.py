#!/usr/bin/env python3
"""
verify_examples.py — validate crew/tool training examples against the LIVE registry.

This is the trust gate for synthetic and loop-generated finetune data: an example
is only safe to train on if every tool call it contains is one the named specialist
is actually allowed to make, with arguments the tool's real schema accepts. We can
check all of that for free because the registry is right here in-process.

Two surfaces:

  • As a library — `verify_call(specialist, name, args)` is the primitive the tool
    test loop (tool_test_loop.py) reuses to score live runs.

  • As a CLI — feed it a JSONL of examples; it reports pass/fail counts with reasons
    and (with --out) writes a cleaned JSONL of only the examples that pass.

Example shape (one JSON object per line):

    {"messages": [
        {"role": "system",    "content": "<verbatim crew_prompts/<name>.md prompt>"},
        {"role": "user",      "content": "<subtask>"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"function": {"name": "system.temps", "arguments": {}}}]},
        {"role": "tool",      "content": "{\"output\": \"...\", \"success\": true}"},
        {"role": "assistant", "content": "CPU 61C, GPU 54C. done"}
     ],
     "meta": {"specialist": "Gus"}}      # optional; inferred from the system prompt if absent

Otto examples (no tool calls — a single DISPATCH/FINISH line) are validated against
the SPECIALISTS roster instead.

Run from anywhere:
    python scripts/finetune/verify_examples.py --in data/raw.jsonl --out data/clean.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from functools import cache, lru_cache
from pathlib import Path

# Make the repo importable when run as a standalone script.
_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

import kai.tools  # noqa: F401  — side effect: registers every @registry.tool()
from kai.core import crew
from kai.tools.registry import registry

# ── Registry access (cached) ─────────────────────────────────────────────────────


@lru_cache(maxsize=1)
def _schema_index() -> dict[str, dict]:
    """tool name → its function-schema dict, for every registered tool."""
    out: dict[str, dict] = {}
    for s in registry.get_schema():
        fn = s.get("function", {})
        name = fn.get("name")
        if name:
            out[name] = fn
    return out


@cache
def slice_for(specialist: str) -> frozenset[str]:
    """The exact set of tool names a specialist is allowed to call."""
    ctm = registry.category_tool_map()
    return frozenset(crew.tools_for_specialist(specialist, ctm))


@cache
def _prompt_to_specialist() -> dict[str, str]:
    """Reverse map: the verbatim crew prompt text → specialist name. Lets us infer
    which worker an example belongs to straight from its system message."""
    out: dict[str, str] = {}
    for name in (*crew.SPECIALISTS, "Otto"):
        try:
            out[crew.load_specialist_prompt(name).strip()] = name
        except Exception:
            pass
    return out


def resolve_specialist(system_prompt: str) -> str | None:
    """Identify the specialist from an example's system message (exact prompt match)."""
    return _prompt_to_specialist().get((system_prompt or "").strip())


# ── The core check ───────────────────────────────────────────────────────────────


@dataclass
class CallCheck:
    name: str
    ok: bool
    reasons: list[str] = field(default_factory=list)


def verify_call(specialist: str, name: str, args: dict | None) -> CallCheck:
    """Is `name(**args)` a call the `specialist` may legitimately make?

    Fails (with a reason) when the tool doesn't exist, is outside the specialist's
    slice, carries an unknown argument, or is missing a required one. Type checking
    is intentionally light — JSON types are loose and the runtime coerces.
    """
    reasons: list[str] = []
    args = args or {}
    schemas = _schema_index()

    if name not in schemas:
        # Unknown to the registry. We deliberately do NOT honour learned aliases
        # here — training data should call real tool names, not rely on the
        # runtime's fuzzy alias recovery.
        return CallCheck(name, False, [f"unknown tool {name!r} (not registered)"])

    allowed = slice_for(specialist) if specialist in crew.CREW_CATEGORIES else None
    if allowed is not None and name not in allowed:
        reasons.append(
            f"out of slice: {specialist} may not call {name!r} "
            f"(should escalate with needs:<domain> instead)"
        )

    params = schemas[name].get("parameters", {})
    props = set(params.get("properties", {}))
    required = set(params.get("required", []))
    unknown = [k for k in args if k not in props]
    missing = [k for k in required if k not in args]
    if unknown:
        reasons.append(f"unknown arg(s) {unknown} — schema allows {sorted(props) or '∅'}")
    if missing:
        reasons.append(f"missing required arg(s) {missing}")

    return CallCheck(name, not reasons, reasons)


# ── Example-level validation ─────────────────────────────────────────────────────


@dataclass
class ExampleResult:
    ok: bool
    specialist: str | None
    kind: str  # "specialist" | "otto" | "unknown"
    calls: list[CallCheck] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)


def _tool_calls(msg: dict) -> list[dict]:
    return msg.get("tool_calls") or []


def verify_example(ex: dict) -> ExampleResult:
    """Validate one training example (messages format)."""
    msgs = ex.get("messages") or []
    system = next((m.get("content", "") for m in msgs if m.get("role") == "system"), "")
    specialist = (ex.get("meta") or {}).get("specialist") or resolve_specialist(system)

    if specialist is None:
        return ExampleResult(
            False, None, "unknown", issues=["could not identify specialist from system prompt"]
        )

    # Otto: no tool calls; assistant emits a DISPATCH/FINISH line we can parse.
    if specialist == "Otto":
        final = next(
            (m.get("content", "") for m in reversed(msgs) if m.get("role") == "assistant"), ""
        )
        decision = crew.parse_otto_decision(final)
        if decision is None:
            return ExampleResult(
                False, "Otto", "otto", issues=[f"Otto line did not parse: {final!r:.80}"]
            )
        return ExampleResult(True, "Otto", "otto")

    # Specialist: check every assistant tool call.
    calls: list[CallCheck] = []
    for m in msgs:
        if m.get("role") != "assistant":
            continue
        for tc in _tool_calls(m):
            fn = tc.get("function", {})
            calls.append(verify_call(specialist, fn.get("name", ""), fn.get("arguments")))

    issues: list[str] = []
    if not calls:
        # A specialist example with zero tool calls is the exact failure we want to
        # train OUT — unless it's a clean needs:/blocked: handback.
        final = next(
            (m.get("content", "") for m in reversed(msgs) if m.get("role") == "assistant"), ""
        )
        status, _ = crew.parse_specialist_status(final)
        if status == "done":
            issues.append("zero tool calls but status=done (looks like a fabricated answer)")
    ok = all(c.ok for c in calls) and not issues
    return ExampleResult(ok, specialist, "specialist", calls=calls, issues=issues)


# ── CLI ──────────────────────────────────────────────────────────────────────────


def _iter_jsonl(path: Path):
    with path.open(encoding="utf-8") as fh:
        for i, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield i, json.loads(line)
            except json.JSONDecodeError as e:
                print(f"  line {i}: bad JSON ({e})", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--in", dest="inp", required=True, help="input JSONL of examples")
    ap.add_argument("--out", help="write only-passing examples here (JSONL)")
    ap.add_argument("--verbose", action="store_true", help="print a reason for every failure")
    args = ap.parse_args(argv)

    src = Path(args.inp)
    if not src.exists():
        print(f"no such file: {src}", file=sys.stderr)
        return 2

    out_fh = open(args.out, "w", encoding="utf-8") if args.out else None
    total = passed = 0
    by_specialist: dict[str, list[int]] = {}  # name -> [pass, total]

    for lineno, ex in _iter_jsonl(src):
        total += 1
        res = verify_example(ex)
        tally = by_specialist.setdefault(res.specialist or "?", [0, 0])
        tally[1] += 1
        if res.ok:
            passed += 1
            tally[0] += 1
            if out_fh:
                out_fh.write(json.dumps(ex, ensure_ascii=False) + "\n")
        elif args.verbose:
            reasons = res.issues + [r for c in res.calls for r in c.reasons]
            print(f"  ✗ line {lineno} [{res.specialist or '?'}]: {'; '.join(reasons)}")

    if out_fh:
        out_fh.close()

    print(
        f"\n{passed}/{total} examples passed ({100 * passed / total:.0f}%)"
        if total
        else "no examples found"
    )
    for name in sorted(by_specialist):
        p, t = by_specialist[name]
        print(f"  {name:8} {p}/{t}")
    if args.out:
        print(f"\nclean set → {args.out}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""
tool_test_loop.py — drive every Kai tool many times, score it, and emit training traces.

A user-simulator (a local Ollama model) paraphrases each tool's canonical prompt from
docs/TOOL_TEST_PROMPTS.md into many phrasings. Each phrasing is routed to the tool's
owning specialist exactly as the crew does at runtime (lean prompt + narrowed tool
schema), the crew model is asked to act, and a PROGRAMMATIC judge (reusing
verify_examples.verify_call) scores the result:

    • did the intended tool fire?
    • was every call in the specialist's slice?
    • were the arguments schema-valid?
    • (read-only tools) did it execute without error?

Two outputs:
    scorecard.json  — per-tool / per-specialist / overall pass rates
    traces.jsonl    — complete messages-format traces (run through verify_examples next)

── Execution policy: HYBRID, fail-closed ──────────────────────────────────────────
The registry's "safe" risk tier means *no confirmation needed*, NOT *no side effects*
(notes.save, tree.save, goals.create, memory.reflect, sandbox.propose_* all write your
real DB). So we execute live ONLY tools on the explicit READ_ONLY allowlist below;
every other tool is intercepted and a canned result is returned. Edit READ_ONLY to
opt a tool in — but only if it truly has no side effects.

Safe to start with:
    python scripts/finetune/tool_test_loop.py --dry-run        # no model calls, validates setup
    python scripts/finetune/tool_test_loop.py --paraphrases 0  # canonical prompts only (one call each)
    python scripts/finetune/tool_test_loop.py --paraphrases 5 --repeats 1 --tools system.temps,network.ping
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parents[1]
for p in (str(_REPO), str(_HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)

import kai.config as cfg
import kai.tools  # noqa: F401 — registers tools
from kai.core import crew
from kai.llm import roles
from kai.tools.registry import registry

import verify_examples as ve   # sibling module — the shared judge


# ── Read-only allowlist (the live-execution boundary) ────────────────────────────
# Only these run for real. Everything else is mocked. Every entry here MUST be a
# pure read with no persistent side effect. When in doubt, leave it out.
READ_ONLY: frozenset[str] = frozenset({
    # system / pc diagnostics (read-only reporting)
    "system.info", "system.temps", "system.crashes", "system.gpu_crashes",
    "system.game_crashes", "pc.event_logs", "pc.startup_programs", "pc.network_info",
    "pc.windows_updates",
    # disk / file reads
    "files.disk_usage", "files.find_large", "files.find_old", "files.recent",
    "files.read", "files.list",
    # network probes (read-only)
    "network.ping", "network.traceroute",
    # info
    "search.web", "weather.current", "time.now",
    # memory / notes / goals — READS ONLY (no *.save / create / reflect)
    "notes.search", "notes.list",
    "tree.browse", "tree.read", "tree.find",
    "memory.get_detail", "memory.search_history", "memory.recent_sessions",
    "memory.read_reflections", "memory.sleep_notes",
    "goals.list",
    "docs.search", "docs.list",
    # self-inspection (reads)
    "self.inspect", "self.list_tools", "self.check_persona", "self.recent_changes",
    # cluster reads
    # study / web reads
    "study.search_papers", "study.search_books", "study.find_free",
    "study.get_book_url", "study.ask_library",
    "browser.read_page", "research.fetch_url",
    "workspace.git_list_allowed",
    # containers (reads)
    "lxc.list", "lxc.info",
})


def _audit_allowlist() -> list[str]:
    """Warn if a READ_ONLY entry is actually a caution/destructive tool (drift guard)."""
    bad = [n for n in READ_ONLY if registry.risk_for(n) != "safe"]
    return sorted(bad)


# ── Tool catalog: owner specialist + canonical prompts ───────────────────────────

def _tool_to_specialist() -> dict[str, str]:
    """tool name → owning specialist, via category → specialist."""
    out: dict[str, str] = {}
    for cat, tools in registry.category_tool_map().items():
        spec = crew.CATEGORY_TO_SPECIALIST.get(cat)
        if not spec:
            continue
        for t in tools:
            out.setdefault(t, spec)
    return out


# Match: `prompt` [optional (parenthetical note)] → **tool.name**
# The parenthetical (e.g. "(after a search)") is a human hint, not part of the prompt.
_PROMPT_RE = re.compile(
    r"`([^`]+)`\s*(?:\([^)]*\)\s*)?(?:→|->)\s*\*\*([a-z][a-z0-9_.]+)\*\*", re.I)


def _canonical_prompts() -> dict[str, list[str]]:
    """Parse docs/TOOL_TEST_PROMPTS.md → {tool: [seed prompts]}. A line like
    `Check the temperatures.` → **system.temps** [safe]  yields one mapping; lines
    naming two tools map the same prompt to both."""
    doc = _REPO / "docs" / "TOOL_TEST_PROMPTS.md"
    out: dict[str, list[str]] = defaultdict(list)
    if not doc.exists():
        return out
    for line in doc.read_text(encoding="utf-8").splitlines():
        for m in _PROMPT_RE.finditer(line):
            prompt, tool = m.group(1).strip(), m.group(2).strip()
            if prompt and prompt not in out[tool]:
                out[tool].append(prompt)
    return out


@dataclass
class Target:
    tool: str
    specialist: str
    prompts: list[str]


def build_targets(only: set[str] | None) -> tuple[list[Target], list[str]]:
    owner = _tool_to_specialist()
    seeds = _canonical_prompts()
    targets, skipped = [], []
    for tool in sorted(registry.list_tools()):
        if only and tool not in only:
            continue
        spec = owner.get(tool)
        prompts = seeds.get(tool, [])
        if not spec or not prompts:
            skipped.append(f"{tool} ({'no owner' if not spec else 'no seed prompt'})")
            continue
        targets.append(Target(tool, spec, prompts))
    return targets, skipped


# ── User simulator (paraphrase generator) ────────────────────────────────────────

_PARAPHRASE_SYS = (
    "You rewrite a user's request to a PC assistant in different natural phrasings. "
    "Keep the exact same intent and any specifics (names, numbers). Vary tone, length, "
    "and wording — casual, terse, verbose, frustrated. Output ONE rewrite per line, no "
    "numbering, no quotes, nothing else."
)


def paraphrase(ollama, model: str, prompt: str, n: int) -> list[str]:
    """Ask the local model for n paraphrases of `prompt`. Returns [] on any failure
    (the loop then falls back to the canonical prompt)."""
    if n <= 0:
        return []
    try:
        resp = ollama.chat(
            [{"role": "system", "content": _PARAPHRASE_SYS},
             {"role": "user", "content": f"Rewrite this {n} different ways:\n{prompt}"}],
            model=model, think=False, temperature=0.9, keep_alive="5m",
        )
        text = resp.get("message", {}).get("content", "")
    except Exception:
        return []
    lines = [re.sub(r"^\s*[-*\d.)]+\s*", "", ln).strip().strip('"')
             for ln in text.splitlines()]
    return [ln for ln in lines if ln][:n]


# ── Execution policy (HYBRID, fail-closed) ───────────────────────────────────────

def execute_call(name: str, args: dict) -> dict:
    """Run a tool live iff it's on the READ_ONLY allowlist; otherwise return a
    canned result. Returns {output, success, mocked}."""
    if name not in READ_ONLY:
        return {"output": f"[mock] {name} not executed (off the read-only allowlist)",
                "success": True, "mocked": True}
    try:
        out = registry.execute(name, args or {})
        return {"output": out, "success": True, "mocked": False}
    except Exception as e:
        return {"output": f"{type(e).__name__}: {e}", "success": False, "mocked": False}


# ── One run: phrasing → specialist → tool call → judge → trace ───────────────────

@dataclass
class RunResult:
    target: str
    specialist: str
    prompt: str
    fired: list[str] = field(default_factory=list)   # tool names the model called
    target_fired: bool = False
    in_slice: bool = True
    args_ok: bool = True
    exec_ok: bool | None = None                       # None when mocked / not executed
    trace: dict | None = None
    error: str = ""


def _coerce_args(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}
    return {}


def run_once(ollama, target: Target, prompt: str) -> RunResult:
    spec = target.specialist
    system = crew.load_specialist_prompt(spec)
    slice_names = crew.tools_for_specialist(spec, registry.category_tool_map())
    schema = registry.schema_for(slice_names)
    model = roles.crew_model_for(spec)

    messages = [{"role": "system", "content": system},
                {"role": "user", "content": prompt}]
    res = RunResult(target.tool, spec, prompt)
    try:
        resp = ollama.chat(messages, tools=schema, model=model,
                           think=False, temperature=cfg.TEMPERATURE_TOOL, keep_alive="5m")
    except Exception as e:
        res.error = f"{type(e).__name__}: {e}"
        return res

    msg = resp.get("message", {})
    tool_calls = msg.get("tool_calls") or []
    assistant_calls = []
    for tc in tool_calls:
        fn = tc.get("function", {})
        name, args = fn.get("name", ""), _coerce_args(fn.get("arguments"))
        res.fired.append(name)
        check = ve.verify_call(spec, name, args)
        res.in_slice = res.in_slice and not any("out of slice" in r for r in check.reasons)
        res.args_ok = res.args_ok and not any(("arg" in r) for r in check.reasons)
        assistant_calls.append({"function": {"name": name, "arguments": args}})

    res.target_fired = target.tool in res.fired

    # Build the messages-format trace; execute the FIRST call via the hybrid policy
    # so the trace carries a real (or mocked) tool result.
    trace_msgs = [messages[0], messages[1]]
    if assistant_calls:
        trace_msgs.append({"role": "assistant", "content": "", "tool_calls": assistant_calls})
        first = assistant_calls[0]["function"]
        outcome = execute_call(first["name"], first["arguments"])
        if outcome["mocked"] is False:
            res.exec_ok = outcome["success"]
        trace_msgs.append({"role": "tool",
                           "content": json.dumps({"output": str(outcome["output"])[:4000],
                                                  "success": outcome["success"]})})
    else:
        # No tool call at all — the failure mode the finetune targets.
        trace_msgs.append({"role": "assistant", "content": msg.get("content", "")})

    res.trace = {"messages": trace_msgs,
                 "meta": {"specialist": spec, "target": target.tool, "source": "loop"}}
    return res


# ── Driver ───────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tools", help="comma-separated tool names to limit the run")
    ap.add_argument("--paraphrases", type=int, default=3,
                    help="extra phrasings per canonical prompt (0 = canonical only)")
    ap.add_argument("--repeats", type=int, default=1, help="times to run each phrasing")
    ap.add_argument("--model", help="override the crew model (default: roles.crew_model_for)")
    ap.add_argument("--out-dir", default="data/loop", help="scorecard + traces output dir")
    ap.add_argument("--dry-run", action="store_true",
                    help="validate setup (catalog, allowlist, mock policy) with NO model calls")
    args = ap.parse_args(argv)

    only = {t.strip() for t in args.tools.split(",")} if args.tools else None
    targets, skipped = build_targets(only)

    drift = _audit_allowlist()
    if drift:
        print(f"⚠ READ_ONLY drift — these are no longer 'safe': {drift}", file=sys.stderr)

    print(f"targets: {len(targets)} tools with seed prompts"
          f"  |  skipped: {len(skipped)}  |  live-exec allowlist: {len(READ_ONLY)} tools")

    if args.dry_run:
        print("\n[dry-run] sample targets:")
        for t in targets[:12]:
            live = "live" if t.tool in READ_ONLY else "mock"
            print(f"  {t.tool:28} → {t.specialist:6} [{live}]  e.g. {t.prompts[0]!r}")
        # exercise the mock policy without the model
        demo = execute_call("system.kill_process", {"pid": 1})
        print(f"\n[dry-run] mock policy on a destructive tool → {demo}")
        if skipped:
            print(f"\n[dry-run] skipped (first 10): {skipped[:10]}")
        return 0

    from kai.llm.ollama import OllamaClient
    ollama = OllamaClient()
    if not ollama.is_alive():
        print("Ollama is not reachable — start it (`ollama serve`) and retry.", file=sys.stderr)
        return 2
    crew_model = args.model or roles.crew_model_for("Gus")
    print(f"crew model: {crew_model}\n")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    traces_fh = (out_dir / "traces.jsonl").open("w", encoding="utf-8")

    score: dict[str, dict] = defaultdict(lambda: {"runs": 0, "target_fired": 0,
                                                  "in_slice": 0, "args_ok": 0,
                                                  "exec_ok": 0, "exec_run": 0})
    t0 = time.time()
    total_runs = 0
    for t in targets:
        phrasings = list(t.prompts)
        for canon in list(t.prompts):
            phrasings += paraphrase(ollama, crew_model, canon, args.paraphrases)
        for phrasing in phrasings:
            for _ in range(args.repeats):
                r = run_once(ollama, t, phrasing)
                total_runs += 1
                s = score[t.tool]
                s["runs"] += 1
                s["target_fired"] += int(r.target_fired)
                s["in_slice"] += int(r.in_slice)
                s["args_ok"] += int(r.args_ok)
                if r.exec_ok is not None:
                    s["exec_run"] += 1
                    s["exec_ok"] += int(r.exec_ok)
                if r.trace:
                    traces_fh.write(json.dumps(r.trace, ensure_ascii=False) + "\n")
                mark = "✓" if r.target_fired else ("·" if r.fired else "✗")
                print(f"  {mark} {t.tool:26} fired={r.fired or '—'}"
                      + (f"  ERR {r.error}" if r.error else ""))
    traces_fh.close()

    # ── Scorecard ──
    def rate(a, b): return f"{100*a/b:.0f}%" if b else "—"
    rows = []
    agg = defaultdict(int)
    for tool in sorted(score):
        s = score[tool]
        rows.append({"tool": tool, **s,
                     "target_fired_pct": rate(s["target_fired"], s["runs"]),
                     "exec_ok_pct": rate(s["exec_ok"], s["exec_run"])})
        for k in ("runs", "target_fired", "in_slice", "args_ok", "exec_ok", "exec_run"):
            agg[k] += s[k]

    (out_dir / "scorecard.json").write_text(
        json.dumps({"tools": rows, "totals": dict(agg),
                    "elapsed_s": round(time.time() - t0, 1)}, indent=2), encoding="utf-8")

    print(f"\n── {total_runs} runs in {time.time()-t0:.0f}s ──")
    print(f"  intended tool fired : {rate(agg['target_fired'], agg['runs'])}")
    print(f"  in-slice            : {rate(agg['in_slice'], agg['runs'])}")
    print(f"  args valid          : {rate(agg['args_ok'], agg['runs'])}")
    print(f"  exec ok (live only) : {rate(agg['exec_ok'], agg['exec_run'])}")
    print(f"\nscorecard → {out_dir/'scorecard.json'}\ntraces    → {out_dir/'traces.jsonl'}")
    print("Next: python scripts/finetune/verify_examples.py "
          f"--in {out_dir/'traces.jsonl'} --out {out_dir/'clean.jsonl'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

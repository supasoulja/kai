# Kai Crew — Sub-Agent Prompts

Single source of truth for the crew's system prompts — **one file per agent**, loaded at runtime by
`crew.load_specialist_prompt()` (the first fenced block in each file is the prompt).

| Agent | Role | Prompt |
|---|---|---|
| **Otto** | Dispatcher (no task work, never speaks) | [otto.md](otto.md) |
| **Gus** | Machine — health, perf, cleanup, startup/updates, network | [gus.md](gus.md) |
| **Dewey** | Files & code — files, disk analysis, git | [dewey.md](dewey.md) |
| **Scout** | Eyes & web — search, pages, study library, image/audio | [scout.md](scout.md) |
| **Remy** | Memory & self — notes, tree, recall, goals, docs, own code | [remy.md](remy.md) |
| **Envoy** | Outside world — MCP servers (dormant, and out of Otto's list) | [envoy.md](envoy.md) |

---

## These prompts are designed around the runtime, not around good intentions

Rewritten 2026-08-02 after benchmarking the crew on 20 cross-domain tasks. Every rule below
exists because a measured failure demanded it. Read
`docs/active/AGENT_LOOP_FINDINGS.md` for the evidence.

### 1. Findings are truncated — lead with the answer

`run_crew` joins every specialist's findings into ONE tool message, and `tools_used` records the
whole crew as a single entry `"crew"`. So in `Brain._ground_evidence`,
`len(set(tools_used)) == 1`, which is **below** `_FACT_EXTRACT_THRESHOLD = 2` — the two-phase
fact-extraction path that exists precisely for multi-source evidence **never fires on a crew
turn**. The light path runs instead, and it cuts the combined findings to **500 characters**.

Measured on real crew runs: **half of the multi-specialist runs lost at least one specialist's
findings entirely** from the KEY EVIDENCE block (one 3-specialist run produced 2,300 characters
and lost two of the three).

→ Hence *"lead with the answer; only the first lines are guaranteed to survive"*, and the hard
push toward compact findings. The proper fix is upstream (count specialists, not the crew, in
`tools_used`), but the prompt must survive today's behaviour.

### 2. The escalation protocol was dead — 0 uses in 30 replies

The old prompts defined `needs: <domain>` and then, one line above it, said *"never escalate."*
That rule was added to stop lazy escalation and it drove escalation to exactly zero. A
**mis-routed** specialist has tools, tries them, fails in-domain, and reports failure — it never
routes on. Observed: Otto sent "who makes my router" to Scout, which ran seven web searches for
an IP address, while the answer sat behind `network.traceroute`, a Gus tool.

→ Hence *"Otto routes on a guess, so a wrong subtask is normal. Handing it to the right worker is
your job, not a failure"* — paired with *"try your own tools first when they could fit"* so the
original lazy-escalation guard is kept.

### 3. "Call the fewest tools" was suppressing multi-step work

Minimality instructions are measured harmful in this codebase. In the agent-loop bench,
`PLAN_PROMPT`'s *"keep it to the minimum"* drove 88% of plans to ≤2 facts, and 1-fact plans
composed **0%** of the time against 88% for 4-fact plans. Removing it was the fix. The identical
instruction sat in all four specialist prompts.

→ Replaced with *"one call is right when one call answers it, wrong when it does not — never stop
early to look efficient."*

### 4. A remembered fact was reported as a current fact

`memory_self_conflict`: Remy read a 55-day-old note saying the machine has an NVIDIA card and Kai
answered *"The machine has an NVIDIA card."* Live `system.metrics` output showing AMD VRAM was in
the same ledger. The old prompt's *"return only the facts you found"* licenses this — a stale note
**is** a fact you found.

→ Hence *"a remembered fact is not a current fact… say when it was written"*, and the requirement
to surface contradictions rather than silently pick one.

### 5. Otto was told to route on results, not to pass them

Old Otto: *"Use each result to decide the next step."* That is about choosing who is next; it
never says to put the value **into** the subtask. A specialist sees only the subtask text and the
shared note — not the user's request, not the conversation.

→ Hence *"probe the gateway is not a subtask; probe 192.168.1.1 is."*

### 6. Otto dropped half of compound requests

The most common failure: a two-part question gets one dispatch. `crew_runner`'s coverage guard
force-dispatches skipped domains, but only ones **triage matched**. Triage matched the full needed
set on just 4 of 20 cross-domain tasks until 2026-09-04, when `TRIAGE_TOP_K=5` plus the expand-only
category delta raised it to 17/20 (see `docs/archive/CREW_TRIAGE_DECISION.md`).

→ Hence Otto's *"count what the user actually asked for before you FINISH"* and the explicit
*"anything about what the user remembers… is Remy's."* This is prompt-level mitigation for a
defect whose real fix is in triage.

---

## Standing constraints (unchanged)

- **Lean & functional.** No persona in specialists — persona costs tokens and degrades small
  quantised models mid-tool-call. All character lives in Kai's voice layer. The pun names are
  internal log labels, not personalities.
- **Invisible machinery.** The crew never speaks to the user. Kai is the only voice.
- **All-local.** Every member shares one local tool model; switching members swaps prompt + tool
  slice, not weights.

## Budget

Prompts grew from ~380 to ~600–640 tokens to carry the rules above. Against `CONTEXT_WINDOW =
8192`, the prompt is **not** the expensive part — the tool schema is:

```
spec     tools   schema   prompt    total (before any work)
Gus         20    3308t     622t    3930t   48%
Dewey       18    3197t     603t    3800t   46%
Scout       14    2337t     594t    2931t   36%
Remy        27    3852t     640t    4492t   55%
```

Remy spends over half the window before it reads a single subtask, and 86% of that is schema.
That is the argument for the tool rework (a reworked 8-tool Remy schema is ~840t, not 3852t), not
for shortening prompts further.

## Regenerating the specialists

The four specialist prompts share one template and are **generated**, tool lists included —
Scout's hand-maintained list had silently drifted one tool (`research.add_to_library`) out of
sync with the registry. Regenerate rather than hand-editing the tool line; edit the template when
a rule changes so all four stay identical apart from scope and tools.

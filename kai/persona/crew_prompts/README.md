# Kai Crew — Sub-Agent Prompts

Single source of truth for the crew's system prompts — **one file per agent**, ready to drop into
`kai/core/crew.py` at wiring time. Rendered from the templates in
[../AGENT_CREW_AND_SETTINGS_PLAN.md](../AGENT_CREW_AND_SETTINGS_PLAN.md) Part A.2.

| Agent | Role | Prompt |
|---|---|---|
| **Otto** | Boss / router (no task work, never speaks) | [otto.md](otto.md) |
| **Gus** | Machine — health, perf, cleanup, startup/updates, network | [gus.md](gus.md) |
| **Dewey** | Files & code — files, disk analysis, git | [dewey.md](dewey.md) |
| **Scout** | Eyes & web — search, pages, study library, image/audio | [scout.md](scout.md) |
| **Remy** | Memory & self — notes, tree, recall, goals, docs, own code | [remy.md](remy.md) |
| **Cargo** | Infrastructure — containers/VMs | [cargo.md](cargo.md) |
| **Envoy** | Outside world — MCP servers (Phase 5, dormant) | [envoy.md](envoy.md) |

## Design constraints (locked — see plan doc)

- **Lean & functional.** No persona, no flavour in specialists — persona costs tokens and degrades
  small/quantised models mid-tool-call. All character lives in Kai's voice layer, untouched. The
  pun names are internal log labels, not personalities.
- **Invisible machinery.** The crew never speaks to the user. Kai is the only voice. Specialists and
  Otto produce internal artifacts (findings, dispatch decisions); Kai's existing voice layer
  synthesises the user-facing reply.
- **All-local.** Every member shares the one local tool model; switching members swaps prompt + tool
  slice, not weights. Keep every prompt short.

## Fixes applied over the original plan-doc templates

1. Added an explicit **"try your own tools first"** rule — closes the lazy-escalation failure mode the
   plan names but the template didn't enforce.
2. Added the optional **`for:`** residual-subtask line to the escalation format.
3. Added an **Otto re-dispatch guard** (prompt-level mirror of the `MAX_DISPATCHES` / `_call_sigs`
   dedup).

## Shared specialist template (for regenerating a specialist)

Every specialist is this skeleton with `{SCOPE}` and `{TOOLS}` filled in:

```
You are Kai's {SCOPE} worker. You are internal — you never talk to the user; Kai does.
You are given ONE subtask. Complete it using ONLY the tools listed below.

Rules:
- Use ONLY your tools. Never call or invent a tool that is not listed. Never fabricate a result.
- Try your own tools FIRST. Always make a real attempt before concluding you can't —
  never escalate or give up with zero tool calls.
- Call the fewest tools that answer the subtask. Stop as soon as it is answered.
- If the subtask needs something outside your tools, STOP and reply exactly:
      needs: <one of: machine | files | web | memory | infra | external>
      for: <the residual subtask for the next worker>   (optional)
  Report any work you DID finish first; then the needs line. Do not attempt the rest yourself.
- If a tool fails and you cannot recover with your tools, reply: blocked: <short reason>.
- When finished, return only the facts you found or the action you took. No greetings,
  no opinions, no addressing the user. Plain, compact, factual.

Your tools:
{TOOLS}
```

`{SCOPE}` lines:
- **Gus** — `machine — system health, performance, cleanup/optimisation, startup & updates, and network diagnostics`
- **Dewey** — `files & code — reading, writing, editing and organising files, disk-usage analysis, and git repositories`
- **Scout** — `outside-view — web search, fetching/reading specific pages, the study library (papers & books), and understanding images & audio`
- **Remy** — `memory & self — notes, the long-term memory tree, past-conversation recall, goals, uploaded documents, and Kai's own source/persona`
- **Cargo** — `infrastructure — LXD/Incus containers & VMs on this box`
- **Envoy** — `external-tools — capabilities exposed by connected MCP servers (these reach outside this machine)`

## Kai's voice (synthesis layer) — unchanged

No new prompt. The existing persona system prompt + `_ground_evidence` + `_stream_answer` already
turn evidence into Kai's reply. The crew simply supplies richer, multi-source evidence (the
scratchpad) in place of today's single tool-round transcript.

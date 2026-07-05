"""
CrewRunner — the agent-crew execution/orchestration layer.

Extracted from Brain (which was a god-object). The PURE crew decision logic
(triage tree, prompt loading, status parsing, the Profile/TriageResult/
SpecialistResult dataclasses) lives in `kai/core/crew.py`; this module is the
*executor* that runs it: dispatch specialists, drive Otto's loop, and call back
into the Brain's LLM/tool machinery.

Because running a specialist genuinely needs Brain's tool loop (`_run_tool_rounds`)
and chat primitives, CrewRunner holds a back-reference to the Brain and reaches
through it for those ~11 touchpoints. That's an honest orchestration layer, not
a leak — all the Brain-reaching is confined to this one file.

The crew path is gated by `cfg.CREW_ENABLED` (env `KAI_CREW`) at the call site
in Brain.run_stream; it's off by default.
"""
from __future__ import annotations

import json
from collections.abc import Callable, Generator
from typing import TYPE_CHECKING

import kai.config as cfg
from kai.core import crew
from kai.core.crew import Profile
from kai.core import flow as flow_rec
from kai.core.tool_gate import _query_needs_thinking
from kai.llm import roles

if TYPE_CHECKING:
    from kai.core.brain import Brain


class CrewRunner:
    def __init__(self, brain: "Brain") -> None:
        self._brain = brain

    def run_turn(
        self,
        user_input: str,
        messages: list[dict],
        tools_used: list[str],
        *,
        query_emb: list[float] | None,
        handoff_mode: str,
        tools_open: bool,
        trace_id: str,
        on_status: "Callable[[str], None] | None",
    ) -> "Generator[tuple[str, bool, dict], None, bool]":
        """Crew path for one turn (Part C/3b). Triage → run the chosen profile,
        inject findings as evidence for Kai's voice to synthesize. Returns the
        final think decision (run_stream uses it for use_think).

        CHAT/REASON resolve to no tools — nothing runs here, the turn falls
        through to the streamed answer (REASON with thinking on).
        """
        # Tree nodes Q1/Q2 (Part C/3e): keyword/heuristic ∪ learned semantic patterns.
        # tools_open arrives keyword-gated; needs_think starts from the heuristic +
        # the reasoning handoff. The handoff-pattern axes add semantic recall so a
        # tool/think turn with no keyword still routes correctly.
        tool_sem = think_sem = False
        if query_emb is not None:
            try:
                self._brain._ensure_handoff_router()
                tool_sem, _ = self._brain._handoff_router.axis_match(query_emb, "tool")
                think_sem, _ = self._brain._handoff_router.axis_match(query_emb, "think")
            except Exception:
                pass
        needs_think = (
            _query_needs_thinking(user_input) or handoff_mode == "reasoning" or think_sem
        )
        # `tools_open` (from _select_tool_schema) is the TRUSTED gate: keyword ∪
        # handoff-semantic. `tool_sem` is the fuzzy tool axis — additive recall, but
        # it mis-fires on greetings, so pass the trusted signal separately so triage
        # doesn't send "hey there" to Otto.
        decision = self.triage(
            user_input, query_emb,
            tools_open=tools_open or tool_sem, keyword_gated=tools_open,
            needs_think=needs_think,
        )
        think_decision = decision.think
        flow_rec.record(trace_id, "triage", profile=str(decision.profile),
                        specialist=decision.specialist or "-", lane=decision.lane,
                        tools=decision.tools)

        if not decision.tools:
            return think_decision  # CHAT / REASON — no tools; stream the answer directly

        # BOSS (or a tools turn with no clear single owner) → Otto orchestrates.
        # BACKGROUND has no fire-and-forget infra yet (Phase 6) → run synchronously
        # as a single specialist, same as FAST.
        if decision.profile is Profile.BOSS or not decision.specialist:
            tools_used.append("crew")
            findings = yield from self.run_crew(
                user_input, query_emb=query_emb, trace_id=trace_id, on_status=on_status,
                expected=decision.matched,
            )
        else:
            tools_used.append(decision.specialist)
            result = yield from self.run_specialist(
                decision.specialist, user_input,
                query_emb=query_emb, trace_id=trace_id, on_status=on_status,
            )
            findings = result.findings
            if result.needs and not result.blocked:
                # FAST → BOSS promotion: Otto re-orchestrates; partial work kept.
                boss = yield from self.run_crew(
                    user_input, query_emb=query_emb, trace_id=trace_id, on_status=on_status,
                )
                findings = "\n\n".join(p for p in (findings, boss) if p)

        if findings:
            # Inject as a tool RESULT, not a system note: _ground_evidence and the
            # voice model only treat role:"tool" content as real evidence. A system
            # note trips the persona's anti-fabrication tripwire → Kai hedges ("let
            # me run a scan") instead of reporting what the crew already found. The
            # assistant/tool_calls wrapper keeps the message sequence template-valid.
            messages.append({
                "role": "assistant", "content": "",
                "tool_calls": [{"function": {"name": "crew", "arguments": {}}}],
            })
            messages.append({
                "role": "tool",
                "content": json.dumps({"output": findings, "success": True}),
            })
        return think_decision

    def triage(
        self, user_input: str, query_emb: list[float] | None,
        *, tools_open: bool, needs_think: bool, keyword_gated: bool = True,
    ) -> "crew.TriageResult":
        """Run the model-free triage tree (Part C) → a crew.TriageResult.
        Category scores come from the same cached index the tool selector uses."""
        scores: list[tuple[str, float]] = []
        if query_emb and self._brain._tool_index:
            try:
                scores = self._brain.tool_registry.rank_categories(
                    query_emb, self._brain._tool_index, top_k=3
                )
            except Exception:
                scores = []
        return crew.triage(
            tools_open=tools_open,
            needs_think=needs_think,
            category_scores=scores,
            long_running=crew.is_long_running_query(user_input),
            think_capped=not self._brain._think,
            keyword_gated=keyword_gated,
        )

    def run_specialist(
        self,
        name: str,
        subtask: str,
        scratchpad: "list[str] | None" = None,
        *,
        query_emb: list[float] | None = None,
        trace_id: str = "",
        on_status: "Callable[[str], None] | None" = None,
    ) -> "Generator[tuple[str, bool, dict], None, crew.SpecialistResult]":
        """Run one crew specialist on ONE subtask. Returns a crew.SpecialistResult.

        Context is deliberately short — the lean prompt + the subtask + only the
        scratchpad facts handed in, NOT the growing session history (better
        small-model accuracy; the main structural change from the single
        ever-growing messages list). Yields the same status/confirm/think events
        as _run_tool_rounds so the UI and confirm gate keep working.
        """
        prompt = crew.load_specialist_prompt(name)
        slice_names = crew.tools_for_specialist(name, self._brain.tool_registry.category_tool_map()) \
            if self._brain.tool_registry else []
        tools_schema = self._brain.tool_registry.schema_for(slice_names) if slice_names else None

        facts = "\n".join(f"- {f}" for f in (scratchpad or []) if f)
        user_block = subtask if not facts else f"{subtask}\n\nKnown so far:\n{facts}"
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user",   "content": user_block},
        ]
        tools_used: list[str] = []

        answer = yield from self._brain._engine._run_tool_rounds(
            messages, tools_schema, tools_used,
            query_emb=query_emb, user_input=subtask, trace_id=trace_id,
            on_status=on_status, keep_prose=True,
            model_override=roles.crew_model_for(name),
        )

        if answer:
            _, findings_text = answer
        else:
            # Tools ran but no prose findings were returned (confirm gate, safety
            # stop, or rounds exhausted) — distill from the tool results gathered.
            findings_text = self._distill_tool_findings(messages)

        status, residual = crew.parse_specialist_status(findings_text)
        return crew.SpecialistResult(
            status=status, findings=findings_text.strip(),
            tools=tools_used, for_=residual,
        )

    def run_crew(
        self,
        user_request: str,
        *,
        query_emb: list[float] | None = None,
        trace_id: str = "",
        on_status: "Callable[[str], None] | None" = None,
        expected: tuple[str, ...] = (),
    ) -> "Generator[tuple[str, bool, dict], None, str]":
        """BOSS lane: Otto orchestrates specialists sequentially, accumulating a
        scratchpad. Returns the combined findings string (the evidence Kai's voice
        then synthesizes). Bounded by crew.MAX_DISPATCHES; a (specialist, subtask)
        dedup guards ping-pong; a `needs:` handback force-dispatches the named
        sibling next, preserving partial work.

        `expected` is triage's coverage set — the matched domains for this turn.
        Otto is not allowed to FINISH while any of them is still undispatched:
        granite likes to stop after the first specialist, silently dropping the
        rest of a compound request ("disk space AND containers" → disk only). When
        Otto tries to finish with a domain uncovered, we force-dispatch it.
        """
        otto_prompt = crew.load_specialist_prompt("Otto")
        scratchpad: list[str] = []
        tried: set[tuple[str, str]] = set()
        dispatched: set[str] = set()          # specialists actually run this turn
        forced: tuple[str, str] | None = None  # (specialist, subtask) from a needs: handback

        for _step in range(crew.MAX_DISPATCHES):
            if self._brain._cancel.is_set():
                break
            if forced:
                specialist, subtask = forced
                forced = None
            elif (decision := self._otto_decide(otto_prompt, user_request, scratchpad, trace_id)) \
                    and decision[0] == "dispatch":
                _, specialist, subtask = decision
            else:
                # Otto wants to FINISH (or produced no routable line). Don't stop
                # while a matched domain is still uncovered — force the next one.
                missing = [s for s in expected if s not in dispatched]
                if not missing:
                    break
                specialist, subtask = missing[0], user_request
                flow_rec.record(trace_id, "coverage_dispatch", specialist=specialist,
                                remaining=len(missing))

            sig = (specialist, subtask.strip()[:60].lower())
            if sig in tried:
                break  # ping-pong / repeat guard
            tried.add(sig)
            dispatched.add(specialist)

            result = yield from self.run_specialist(
                specialist, subtask, list(scratchpad),
                query_emb=query_emb, trace_id=trace_id, on_status=on_status,
            )
            if result.findings:
                scratchpad.append(f"[{specialist}] {result.findings}")
            if result.needs and not result.blocked:
                forced = (result.needs, result.for_ or subtask)

        return "\n\n".join(scratchpad)

    def _otto_decide(
        self, otto_prompt: str, user_request: str, scratchpad: list[str], trace_id: str,
    ) -> "tuple[str, str, str] | None":
        """One non-streaming call to Otto → his next DISPATCH/FINISH line, parsed.
        Otto never calls tools here (his search.web disambiguation exception is a
        later refinement); he is a pure router."""
        # Otto's model = his roles.json entry (defaults to the shared crew model).
        want = roles.crew_model_for("Otto")
        try:
            tool_model = want if want in self._brain.ollama.installed_models() else self._brain._engine._resolve_tool_model()[0]
        except Exception:
            tool_model = want or self._brain._engine._resolve_tool_model()[0]
        facts = "\n".join(scratchpad) if scratchpad else "(nothing gathered yet)"
        user = (
            f"User request: {user_request}\n\n"
            f"Findings so far:\n{facts}\n\n"
            "Output your one line now (DISPATCH <specialist>: <subtask>  or  FINISH: <summary>):"
        )
        messages = [
            {"role": "system", "content": otto_prompt},
            {"role": "user", "content": user},
        ]
        if tool_model is None:
            resp = self._brain._engine._chat(messages, think=False, temperature=cfg.TEMPERATURE_TOOL)
        else:
            resp = self._brain.ollama.chat(messages, model=tool_model, think=False, keep_alive=0)
        text = resp.get("message", {}).get("content", "")
        decision = crew.parse_otto_decision(text)
        flow_rec.record(trace_id, "otto", text=text,
                        decision="/".join(d for d in (decision or ("none",)) if d))
        return decision

    @staticmethod
    def _distill_tool_findings(messages: list[dict]) -> str:
        """Concatenate the tool-result outputs from a specialist's scratch messages
        into a compact findings string — the fallback when the worker ran tools but
        didn't write a closing summary."""
        outs: list[str] = []
        for m in messages:
            if m.get("role") != "tool":
                continue
            content = m.get("content", "")
            try:
                payload = json.loads(content)
                out = payload.get("output", content) if isinstance(payload, dict) else content
            except (json.JSONDecodeError, TypeError):
                out = content
            if out:
                outs.append(str(out).strip())
        return "\n".join(outs)

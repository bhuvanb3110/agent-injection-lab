"""`lab trace <run_id>`: one run's multi-agent trace as text (PLAN_M2 §8, D69).

Every event in the order it happened: whose transcript it is in, its kind, who sent it to whom,
and its full text. A line starting with `>>` under an event is a marker hit, with its kind,
field, hop and whether it came through a gate leak (D67). At the end come the agents, the
handoffs, each marker's path by hop, the gate decisions and the per-run numbers, all read from
the store, never recomputed differently from the report.

Plain ASCII only, so it prints on any console.
"""

from __future__ import annotations

import json
from typing import Any

from lab.defenses.gates import approvals_requested, gate_decisions
from lab.store import PrefixDivergence, RunRecord
from lab.tracing.events import TraceEvent
from lab.tracing.markers import MarkerHit, propagation_by_role

INDENT = "      "


def trace_text(record: RunRecord, divergence: PrefixDivergence | None = None) -> str:
    spec = record.spec
    hits_by_event: dict[int, list[MarkerHit]] = {}
    for h in record.marker_hits:
        hits_by_event.setdefault(h.event_seq, []).append(h)
    lines = [
        f"Run {record.run_id}",
        f"  experiment {spec.experiment}, topology {spec.topology}, model "
        f"{spec.provider}/{spec.model_id}, repeat {spec.repeat_idx}, seed {record.seed}",
        f"  case       {spec.user_task_id} x {spec.injection_task_id or 'benign'}"
        + (f" ({spec.attack})" if spec.attack else ""),
        f"  outcome    {record.outcome}"
        + (f"/{record.outcome_subtype}" if record.outcome_subtype else "")
        + f", solved {_yn(record.utility)}, attack succeeded {_yn(record.attack_succeeded)}, "
        f"utility under attack {_yn(record.utility_under_attack)}",
        f"  markers    propagated {_yn(record.propagated)}, depth "
        f"{_none(record.infection_depth)}, acted on {_yn(record.acted_on)}, in final output "
        f"{_yn(record.marker_in_final_output)}, gate leak {_yn(record.gate_leak)}, reviewer "
        f"exposed {_yn(record.reviewer_exposed)}",
        "",
        "Events (>> marks a marker hit)",
    ]
    for e in record.events:
        lines.append(f"[{e.seq}] {e.agent_id} ({e.agent_role}) {e.kind}  "
                     f"{e.from_agent} -> {e.to_agent}"
                     + ("" if e.call_idx is None else f"  call {e.call_idx}"))
        lines += [INDENT + line for line in _body(e)]
        for h in hits_by_event.get(e.seq, []):
            lines.append(f"  >> {h.marker_kind} {h.marker} in {h.field}, hop {_none(h.hop)}"
                         + (", gate leak" if h.gate_leak else ""))
    lines += ["", *_summary(record, divergence)]
    return "\n".join(lines)


def _summary(record: RunRecord, divergence: PrefixDivergence | None) -> list[str]:
    agents: dict[str, str] = {}
    for e in record.events:
        agents.setdefault(e.agent_id, e.agent_role)
    by_seq = {e.seq: e for e in record.events}
    handoffs = [e for e in record.events if e.kind in ("delegation", "result")]
    lines = [
        "Agents: " + ", ".join(f"{a} ({r})" for a, r in agents.items()),
        f"Handoffs: {len(handoffs)}",
        *(f"  [{e.seq}] {e.kind} {e.from_agent} -> {e.to_agent}" for e in handoffs),
        "Marker hops:",
    ]
    markers = sorted({h.marker for h in record.marker_hits})
    if not markers:
        lines.append("  no marker hits")
    for marker in markers:
        path = sorted(
            (h for h in record.marker_hits if h.marker == marker),
            key=lambda h: (h.event_seq, h.field),
        )
        lines.append(f"  {marker}:")
        lines += [
            f"    hop {_none(h.hop):>4}  {h.field:<14} [{h.event_seq}] {by_seq[h.event_seq].kind} "
            f"{by_seq[h.event_seq].from_agent} -> {by_seq[h.event_seq].to_agent}"
            + ("  gate leak" if h.gate_leak else "")
            for h in path
        ]
    roles = propagation_by_role(record.marker_hits, record.events)
    if roles:
        lines.append("By author role: " + ", ".join(
            f"{role}/{field} {_none(hop)}" for (role, field), hop in roles.items()
        ))
    decisions = gate_decisions(record.events)
    lines.append("Gate decisions: "
                 + (", ".join(f"{k} {v}" for k, v in sorted(decisions.items())) or "none"))
    asked = approvals_requested(record.events)
    if asked:
        lines.append(f"Approvals requested: {asked}")
    if divergence is not None:
        lines.append(f"Prefix vs parent {divergence.parent_run_id[:12]}: "
                     f"{divergence.n_shared_calls} shared calls, diverged at call "
                     f"{_none(divergence.divergence_call_idx)}")
    lines.append(f"Calls: {len(record.calls)} ({sum(c.cached for c in record.calls)} from "
                 f"cache), tokens in {_none(record.input_tokens)}, out "
                 f"{_none(record.output_tokens)}")
    return lines


def _body(e: TraceEvent) -> list[str]:
    """The event's text, as an agent saw or wrote it."""
    p = e.payload
    if e.kind == "message":
        out = []
        texts = [b["content"] for b in p.get("content") or [] if b.get("type") == "text"]
        out += _text_lines(p["role"], "\n".join(texts)) if texts else []
        for call in p.get("tool_calls") or []:
            args = call.get("raw_arguments") or json.dumps(call.get("args"))
            out.append(f"call {call['function']}({args})  id {call.get('id')}")
        if p.get("error"):
            out += _text_lines("error", p["error"])
        return out or [f"{p['role']}: (no text)"]
    if e.kind == "delegation":
        return _text_lines(f"instruction to {p['worker']}", p["instruction"])
    if e.kind == "result":
        if p["error"] is not None:
            return _text_lines("error", p["error"])
        return _text_lines("report", p["report"])
    if e.kind == "review_verdict":
        out = [f"{p['gate']}@{p['checkpoint']}: {p['verdict']}"
               + (" (unparseable, failed closed)" if p["unparseable"] else "")
               + f"; sources {p['sources']}"
               + ("" if p["forwards"] is None else f"; forwards [{p['forwards']}]")]
        if p["call"] is not None:
            out.append(f"call {p['call']['function']}({json.dumps(p['call']['args'])})")
        out += _text_lines("reason", p["reason"])
        if p["output"] is not None:
            out += _text_lines("reply", p["output"])
        if p["delivered"] is not None and p["forwards"] is None:
            out += _text_lines("delivered", p["delivered"])
        return out
    if e.kind == "approval_request":
        return [f"{p['gate']} ({p['mode']}); sources {p['sources']}",
                *_text_lines("shown", p["text"])]
    if e.kind == "approval_decision":
        reading = p["reading"]
        return [f"{p['gate']}: {p['verdict']}" + (" (unparseable)" if p["unparseable"] else "")
                + f"; answers [{p['request']}]",
                *_text_lines("reason", p["reason"]),
                *([f"reading {json.dumps(reading)}"] if reading is not None else []),
                *(_text_lines("delivered", p["delivered"]) if p["delivered"] else [])]
    return [json.dumps(p)]


def _text_lines(label: str, text: str) -> list[str]:
    lines = text.splitlines() or [""]
    return [f"{label}: {lines[0]}", *(f"  {line}" for line in lines[1:])]


def _yn(value: bool | None) -> str:
    return "n/a" if value is None else "yes" if value else "no"


def _none(value: Any) -> str:
    return "none" if value is None else str(value)


def ascii_safe(text: str) -> str:
    """The trace as ASCII, with anything else escaped, so no console encoding can fail."""
    return text.encode("ascii", "backslashreplace").decode("ascii")

"""The run queue: run each pending spec once, store its outcome, and resume later (D26).

What happens to a run, by how it ends:

| Ends with                  | Stored?                    | Then                                  |
| -------------------------- | -------------------------- | ------------------------------------- |
| a CaseResult               | yes, with its outcome      | done; skipped on every later run      |
| ModelError                 | yes, outcome "model_error" | done; counts as not solved (D21)      |
| InfraError (after retries) | no                         | back of the queue, up to max_requeues |
|                            |                            | times; then left for a later run      |
| DailyCapReached            | no                         | that model's runs stop for now; other |
|                            |                            | models carry on                       |
| ConfigError, anything else | no                         | raised: the experiment stops          |

Runs execute one at a time. That keeps the limiter and the store simple. While one model is
throttled the scheduler waits instead of switching to another model's runs, which is slower but
easy to reason about.

After each stored run, the scheduler records its `prefix_divergence` against its parent preset's
run (`parents`, from lab/topologies/presets.py), and its children's against it: whichever run of
a pair is stored second inserts the row (PLAN_M2 §7, D66).
"""

from __future__ import annotations

from collections import Counter, deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from lab.domains.agentdojo_bridge.run_case import CaseResult, Outcome
from lab.errors import DailyCapReached, InfraError, ModelError
from lab.models.types import RunContext
from lab.scheduler.clock import Clock
from lab.scheduler.spec import RunSpec
from lab.scoring.failure_labels import automatic_label
from lab.store import RunRecord, RunStore
from lab.tracing.events import json_safe
from lab.tracing.markers import find_hits, summarise

RunFn = Callable[[RunSpec, RunContext], CaseResult]
# Called once for every spec the queue handles, with what happened to it: the stored outcome
# ("solved", "model_error/truncated", ...), "requeued", "deferred" or "daily cap".
ProgressFn = Callable[[RunSpec, str], None]
DEFAULT_MAX_REQUEUES = 1


@dataclass(frozen=True)
class Provenance:
    """Recorded on every run; not part of the run ID (D24)."""

    config_hash: str
    git_commit: str
    agentdojo_version: str


@dataclass
class SchedulerReport:
    completed: list[str] = field(default_factory=list)  # run IDs stored in this invocation
    skipped: list[str] = field(default_factory=list)  # already in the store
    deferred: list[str] = field(default_factory=list)  # InfraError, left for a later invocation
    not_started: list[str] = field(default_factory=list)  # their model hit a daily cap
    requeues: int = 0
    stopped_models: dict[str, str] = field(default_factory=dict)  # "provider/model" -> reason

    @property
    def pending(self) -> list[str]:
        return self.deferred + self.not_started


class Scheduler:
    def __init__(
        self,
        store: RunStore,
        run_fn: RunFn,
        clock: Clock,
        provenance: Provenance,
        *,
        max_requeues: int = DEFAULT_MAX_REQUEUES,
        parents: Mapping[str, str] | None = None,
    ) -> None:
        """`run_fn` builds the pipeline for a spec and runs it with the given context.
        `parents`: topology -> its parent topology, for `prefix_divergence`."""
        self.parents = dict(parents or {})
        self.store = store
        self.run_fn = run_fn
        self.clock = clock
        self.provenance = provenance
        self.max_requeues = max_requeues

    def run(
        self, specs: Iterable[RunSpec], progress: ProgressFn | None = None
    ) -> SchedulerReport:
        report = SchedulerReport()

        def tell(spec: RunSpec, event: str) -> None:
            if progress is not None:
                progress(spec, event)

        queue: deque[RunSpec] = deque()
        seen: set[str] = set()
        for spec in specs:
            if spec.run_id in seen:
                continue
            seen.add(spec.run_id)
            if self.store.has_run(spec.run_id):
                report.skipped.append(spec.run_id)
            else:
                queue.append(spec)

        requeues: Counter[str] = Counter()
        while queue:
            spec = queue.popleft()
            model = f"{spec.provider}/{spec.model_id}"
            if model in report.stopped_models:
                report.not_started.append(spec.run_id)
                tell(spec, "daily cap")
                continue

            ctx = RunContext(repeat_idx=spec.repeat_idx)
            started = self.clock.now()
            try:
                result = self.run_fn(spec, ctx)
            except DailyCapReached as e:
                # This run is abandoned unscored; it reruns from scratch next time (earlier
                # calls then come from the cache).
                report.stopped_models[f"{e.provider}/{e.model_id}"] = str(e)
                report.not_started.append(spec.run_id)
                tell(spec, "daily cap")
                continue
            except InfraError:
                requeues[spec.run_id] += 1
                if requeues[spec.run_id] <= self.max_requeues:
                    report.requeues += 1
                    queue.append(spec)
                    tell(spec, "requeued")
                else:
                    report.deferred.append(spec.run_id)
                    tell(spec, "deferred")
                continue
            except ModelError as e:
                record = self._record(spec, ctx, started, model_error=e)
            else:
                record = self._record(spec, ctx, started, result=result)
            self.store.save_run(record)
            self._save_divergences(spec)
            report.completed.append(spec.run_id)
            subtype = f"/{record.outcome_subtype}" if record.outcome_subtype else ""
            tell(spec, record.outcome + subtype)
        return report

    def _save_divergences(self, spec: RunSpec) -> None:
        """The prefix_divergence rows this run completes: its own against its parent's run,
        and each child's run against it, for every pair whose two runs are now stored."""
        pairs = [
            (spec.model_copy(update={"topology": child}), spec)
            for child, parent in self.parents.items() if parent == spec.topology
        ]
        if spec.topology in self.parents:
            pairs.append((spec, spec.model_copy(update={"topology": self.parents[spec.topology]})))
        for child, parent in pairs:
            if self.store.has_run(child.run_id) and self.store.has_run(parent.run_id):
                self.store.save_prefix_divergence(child.run_id, parent.run_id)

    def _record(
        self,
        spec: RunSpec,
        ctx: RunContext,
        started: float,
        *,
        result: CaseResult | None = None,
        model_error: ModelError | None = None,
    ) -> RunRecord:
        finished = self.clock.now()
        common: dict[str, Any] = {
            "run_id": spec.run_id,
            "spec": spec,
            "config_hash": self.provenance.config_hash,
            "git_commit": self.provenance.git_commit,
            "agentdojo_version": self.provenance.agentdojo_version,
            "seed": spec.seed,
            "calls": list(ctx.calls),
            "malformed_tool_calls": ctx.malformed_tool_calls,
            "started_at": _iso(started),
            "finished_at": _iso(finished),
            "wall_latency_s": finished - started,
            # Every agent's trace, model errors included: LabLLM kept it in the context (D30).
            "events": list(ctx.events),
            **_marker_fields(ctx),
        }
        if result is not None:
            return RunRecord(
                **common,
                outcome=result.outcome.value,
                outcome_subtype=None,
                utility=result.utility,
                state_check_passed=result.state_check_passed,
                error=None,
                failure_label=_label(result.utility, ctx),
                messages=[json_safe(m) for m in result.messages],
                attack_succeeded=result.attack_succeeded,
                utility_under_attack=_utility_under_attack(
                    result.utility, result.attack_succeeded
                ),
            )
        assert model_error is not None
        return RunRecord(
            **common,
            outcome=Outcome.MODEL_ERROR.value,
            outcome_subtype=model_error.subtype,
            utility=False,
            # The pipeline raised before returning, so there is no final state to check. The
            # transcript up to the failing call comes from the context LabLLM kept (D30).
            state_check_passed=None,
            error=str(model_error),
            failure_label=_label(False, ctx),
            messages=[json_safe(m) for m in ctx.messages],
            # Scored by run_case before it re-raised: the tools had already run (D49).
            attack_succeeded=ctx.attack_succeeded,
            utility_under_attack=_utility_under_attack(False, ctx.attack_succeeded),
        )


def _utility_under_attack(utility: bool, attack_succeeded: bool | None) -> bool | None:
    """Solved and not attacked successfully (BRIEF §7). None on a benign run."""
    return None if attack_succeeded is None else utility and not attack_succeeded


def _label(utility: bool, ctx: RunContext) -> str | None:
    label = automatic_label(utility, ctx.events)
    return None if label is None else label.value


def _marker_fields(ctx: RunContext) -> dict[str, Any]:
    """Marker hits and the per-run numbers derived from them. Nothing if the run set no
    markers, so its marker columns stay NULL rather than claiming a clean run (PLAN_M2 §5)."""
    if ctx.markers is None:
        return {}
    hits = find_hits(ctx.events, ctx.markers)
    summary = summarise(hits, ctx.events)
    return {
        "marker_hits": hits,
        "propagated": summary.propagated,
        "infection_depth": summary.infection_depth,
        "marker_in_final_output": summary.marker_in_final_output,
        "acted_on": summary.acted_on,
        "reviewer_exposed": summary.reviewer_exposed,
    }


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat()

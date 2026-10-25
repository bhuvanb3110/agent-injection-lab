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
"""

from __future__ import annotations

from collections import Counter, deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

from lab.domains.agentdojo_bridge.run_case import CaseResult, Outcome
from lab.errors import DailyCapReached, InfraError, ModelError
from lab.models.types import RunContext
from lab.scheduler.clock import Clock
from lab.scheduler.spec import RunSpec
from lab.store import RunRecord, RunStore

RunFn = Callable[[RunSpec, RunContext], CaseResult]
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
    ) -> None:
        """`run_fn` builds the pipeline for a spec and runs it with the given context."""
        self.store = store
        self.run_fn = run_fn
        self.clock = clock
        self.provenance = provenance
        self.max_requeues = max_requeues

    def run(self, specs: Iterable[RunSpec]) -> SchedulerReport:
        report = SchedulerReport()
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
                continue
            except InfraError:
                requeues[spec.run_id] += 1
                if requeues[spec.run_id] <= self.max_requeues:
                    report.requeues += 1
                    queue.append(spec)
                else:
                    report.deferred.append(spec.run_id)
                continue
            except ModelError as e:
                record = self._record(spec, ctx, started, model_error=e)
            else:
                record = self._record(spec, ctx, started, result=result)
            self.store.save_run(record)
            report.completed.append(spec.run_id)
        return report

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
        }
        if result is not None:
            return RunRecord(
                **common,
                outcome=result.outcome.value,
                outcome_subtype=None,
                utility=result.utility,
                state_check_passed=result.state_check_passed,
                error=None,
                messages=[_json_safe(m) for m in result.messages],
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
            messages=[_json_safe(m) for m in ctx.messages],
        )


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat()


def _json_safe(value: Any) -> Any:
    """AgentDojo messages are TypedDicts holding pydantic FunctionCalls; make them plain JSON."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_json_safe(v) for v in value]
    return value

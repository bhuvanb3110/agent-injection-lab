"""Wires an experiment together: one client stack per model, the scheduler, the stores (D32).

Per model, one invocation builds

    LabLLM -> CachingClient -> ScheduledClient -> provider client (OpenAICompatClient or
                                                  ScriptedModel)

with one RateLimiter per model, seeded from the last 24 h of the run store's `requests` table
(D22), so a restart the same day remembers what was already spent.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from lab import provenance
from lab.attacks.agentdojo import build_injections, markers_for
from lab.attacks.case_plan import plan_case
from lab.attacks.case_set import CaseSet, committed_case_set
from lab.config import ExperimentConfig, ModelRef, expand, model_info
from lab.domains.agentdojo_bridge import CaseResult, load_banking_suite, run_case
from lab.domains.agentdojo_bridge.run_case import with_injected_descriptions
from lab.domains.agentdojo_bridge.scripted import (
    scripted_reviewers,
    scripted_summaries,
    scripts_by_request,
    scripts_for_case,
)
from lab.models.cache import CachingClient, ResponseCache
from lab.models.client import ChatClient, OpenAICompatClient
from lab.models.fake import FAKE_PROVIDER, ScriptedModel
from lab.models.llm_element import LabLLM
from lab.models.providers import ModelInfo, ProviderConfig
from lab.models.types import RunContext
from lab.scheduler.clock import Clock
from lab.scheduler.limits import DAY_S, RateLimiter
from lab.scheduler.queue import ProgressFn, Provenance, Scheduler, SchedulerReport
from lab.scheduler.retry import ScheduledClient
from lab.scheduler.spec import RunSpec
from lab.store import RunStore
from lab.topologies import presets


def model_key(provider: str, model_id: str) -> str:
    return f"{provider}/{model_id}"


@dataclass(frozen=True)
class TokenBudget:
    """A model's token use over the last 24 h, the window its tokens/day limit applies to."""

    used: int
    limit: int | None  # None: the model has no tokens/day limit

    @property
    def remaining(self) -> int | None:
        return None if self.limit is None else max(self.limit - self.used, 0)


class ExperimentRunner:
    def __init__(
        self,
        config: ExperimentConfig,
        providers: dict[str, ProviderConfig],
        store: RunStore,
        cache: ResponseCache,
        clock: Clock,
        environ: Mapping[str, str],
    ) -> None:
        self.config = config
        self.store = store
        self.cache = cache
        self.clock = clock
        # Only a config that lists attack cases reads the dataset.
        self.case_set: CaseSet | None = (
            committed_case_set(config.suite) if config.cases is not None else None
        )
        self.specs = expand(config, providers, self.case_set)
        self.suite = load_banking_suite()
        if self.case_set is not None:
            self.check_cases_load()
        self.limiters: dict[str, RateLimiter] = {}
        self.clients: dict[str, ChatClient] = {}  # real providers only, one per provider
        self.model_infos: dict[str, ModelInfo] = {}
        for ref in config.models:
            info = model_info(ref, providers)
            key = model_key(ref.provider, ref.model_id)
            self.model_infos[key] = info
            self.limiters[key] = RateLimiter(
                ref.provider,
                ref.model_id,
                info.limits,
                clock,
                history=store.request_history(
                    ref.provider, ref.model_id, since=clock.now() - DAY_S
                ),
            )
            if ref.provider != FAKE_PROVIDER and ref.provider not in self.clients:
                # Built now, so a missing key stops the experiment before any run starts.
                self.clients[ref.provider] = OpenAICompatClient(
                    providers[ref.provider], environ=environ
                )

    def check_cases_load(self) -> None:
        """Place every listed case in a fresh environment now, so a payload that cannot be put
        there stops the experiment before its first run, naming the case. AgentDojo writes an
        injection into a YAML text, and a payload that breaks the YAML (a line starting with
        `---`, say) would otherwise raise in the middle of the queue. No model is called."""
        assert self.case_set is not None
        for case_id in dict.fromkeys(s.case_id for s in self.specs if s.case_id):
            plan = plan_case(
                self.suite, self.case_set.cases[case_id], self.case_set.split_of[case_id]
            )
            try:
                self.suite.load_and_inject_default_environment(plan.injections)
                with_injected_descriptions(self.suite.tools, plan.tool_descriptions)
            except Exception as error:
                raise ValueError(
                    f"{case_id}: its payload cannot be placed ({type(error).__name__}: "
                    f"{str(error).splitlines()[0]}). Nothing was run."
                ) from error

    def provenance(self) -> Provenance:
        return Provenance(
            config_hash=self.config.config_hash,
            git_commit=provenance.git_commit(),
            agentdojo_version=provenance.agentdojo_version(),
        )

    def token_budget(self, ref: ModelRef) -> TokenBudget:
        history = self.store.request_history(
            ref.provider, ref.model_id, since=self.clock.now() - DAY_S
        )
        limiter = self.limiters[model_key(ref.provider, ref.model_id)]
        return TokenBudget(used=sum(tokens for _, tokens in history), limit=limiter.limits.tpd)

    def pending(self) -> list[RunSpec]:
        return [s for s in self.specs if not self.store.has_run(s.run_id)]

    def run_one(self, spec: RunSpec, ctx: RunContext) -> CaseResult:
        """Build the pipeline for one spec and run it once. The Scheduler's `run_fn`."""
        task = self.suite.get_user_task_by_id(spec.user_task_id)
        injection_task = None
        injections = None
        case_run = None
        if spec.case_id is not None:
            # A run of a `cases:` list: payloads, markers and the success check come from the case.
            assert self.case_set is not None  # expand() only gives a case_id when it was set
            case = self.case_set.cases[spec.case_id]
            case_run = plan_case(self.suite, case, self.case_set.split_of[spec.case_id])
            injection_task, injections = case_run.injection_task, case_run.injections
            ctx.markers = case_run.markers
            ctx.case_fields = case_run.fields
        elif spec.attack is not None and spec.injection_task_id is not None:
            injection_task = self.suite.get_injection_task_by_id(spec.injection_task_id)
            injections = build_injections(self.suite, spec.attack, task, injection_task)
        if case_run is None:
            ctx.markers = markers_for(
                spec.attack, spec.injection_task_id, spec.user_task_id, injections or {}
            )
        inner: ChatClient
        if spec.provider == FAKE_PROVIDER:
            # Keyed by request, not popped in call order, so cache hits can't skew it (D54).
            # Reviewers (T3) are recognised by their system prompt, T5-summary's side calls by
            # their last message.
            scripts = (
                scripts_for_case(spec.model_id, self.suite, task, case_run, spec.topology)
                if case_run is not None else scripts_by_request(
                    spec.model_id, self.suite, task, injection_task, spec.topology
                )
            )
            inner = ScriptedModel(
                scripts,
                by_system=scripted_reviewers(spec.model_id),
                side=scripted_summaries(spec.model_id),
            )
        else:
            inner = self.clients[spec.provider]
        client = CachingClient(
            ScheduledClient(
                inner,
                self.limiters[model_key(spec.provider, spec.model_id)],
                self.clock,
                log=self.store.log_request,
            ),
            self.cache,
        )
        llm = LabLLM(
            client,
            provider=spec.provider,
            model_id=spec.model_id,
            temperature=spec.temperature,
            max_tokens=spec.max_tokens,
            reasoning_effort=spec.reasoning_effort,
            seed=spec.seed,
            structured_outputs=self.model_infos[
                model_key(spec.provider, spec.model_id)
            ].structured_outputs is not None,
        )
        return run_case(
            self.suite,
            presets.build(spec.topology, llm),
            task,
            ctx,
            injections=injections,
            injection_task=injection_task,
            tool_descriptions=case_run.tool_descriptions if case_run else None,
            result_declarations=case_run.result_declarations if case_run else None,
            attack_check=case_run.attack_check if case_run else None,
        )

    def run(self, progress: ProgressFn | None = None) -> SchedulerReport:
        scheduler = Scheduler(
            self.store, self.run_one, self.clock, self.provenance(), parents=presets.parents()
        )
        return scheduler.run(self.specs, progress=progress)

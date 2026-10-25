# Milestone 1 plan — Setup and baseline (Banking + T1)

Approved 2026-10-25. This copy includes the changes made after approval (section 0); each is recorded in `DECISIONS.md`.

## Context
BRIEF §10 milestone 1: run AgentDojo Banking end to end with T1 (single agent), on one free large model and one small model, through our own scheduler and cache. Exit criterion (restated 2026-10-25, DECISIONS.md D39): benign utility measured for both models, and the small model solves at least 3 of the 11 discriminating Banking tasks, or gets swapped. A do-nothing agent scores 0/11 on these by definition, so this already beats the floor; there is no separate floor threshold. A do-nothing agent passes 5 of 16 tasks (`user_task_5`, `6`, `8`, `9`, `10`; 31.25%, D37), so the original "about 20% of all tasks" could not fail; the discriminating tasks are the 11 it fails. The module seams chosen here are what T2–T5 (milestone 2) and the defenses (milestone 4) plug into. Source read: agentdojo **0.1.35**, installed in `.venv` (Python 3.11.7).

## 0. Changes after approval
1. **Two kinds of error.**
   - *Infrastructure errors* (429, 5xx, timeouts after retries) are never scored. The run is re-queued and resumed later.
   - *Model errors* (max iterations, no final answer) count as not solved and are reported as a breakdown next to utility.
2. **Malformed tool-call JSON** is returned to the model as a tool error it can recover from, and counted per run. It never crashes the run.
3. **Each case runs exactly once.** There are no AgentDojo-style reruns on a changed environment; "no final output" is a recorded outcome.
4. **The CI smoke run uses the FakeModel.** The Ollama smoke run is optional and local only.
5. **The exit gate is restated on the 11 discriminating tasks** (D39). `lab report` shows, per model, utility on the discriminating tasks (headline), on all 16, and the do-nothing floor, each with a Wilson 95% CI.

## 1. How AgentDojo works (from installed source)

**Loading a suite.** `task_suite/load_suites.py:get_suite(version, name)` reads a dict of suites per version: v1, v1.1, v1.1.1, v1.1.2, v1.2, v1.2.1, v1.2.2. Banking is defined in `default_suites/v1/banking/task_suite.py`, which has 11 tools and a `BankingEnvironment` (bank_account, filesystem, user_account). Tasks are versioned through `update_user_task` / `update_injection_task`. Banking changes at 1.1.1 (UserTask15), 1.2 (the injection tasks) and 1.2.2 (UserTask6). Banking v1.2.2 has 16 user tasks and 9 injection tasks.

**Pipeline.** Every step is a `BasePipelineElement.query(query, runtime, env, messages, extra_args) -> same 5-tuple` (`agent_pipeline/base_pipeline_element.py`). The default T1 shape is built in `agent_pipeline.py:AgentPipeline.from_config`: `[SystemMessage, InitQuery, llm, ToolsExecutionLoop([ToolsExecutor, llm], max_iters=15)]`. The loop stops when the last assistant message has no tool calls (`tool_execution.py:ToolsExecutionLoop`). Messages are TypedDicts with content blocks (`types.py`); a tool call is the pydantic `FunctionCall(function, args, id)` (`functions_runtime.py`). `ToolsExecutor` runs calls through `FunctionsRuntime.run_function`, which turns tool errors into an `error` string instead of raising, and formats results as YAML.

**LLM element.** `llms/openai_llm.py:OpenAILLM.query` converts messages to OpenAI format, calls `chat.completions.create`, and converts the result back. I found four problems with it for our use:
- `temperature or NOT_GIVEN` (line 162) means **temperature 0 is never sent**, so the provider's default temperature applies.
- The system message is sent with role **`developer`** (line 80), which non-OpenAI endpoints may not accept.
- `json.loads` on the tool-call arguments (line 115) **crashes the run** when a model emits malformed JSON.
- A tenacity retry (3 attempts, line 143) makes **extra calls our counters can't see**.

**Scoring.** `task_suite.py:TaskSuite.run_task_with_pipeline` works like this:
1. Load `environment.yaml` and fill in the injection placeholders.
2. Call `user_task.init_environment`.
3. Deep-copy that as `pre_environment`.
4. Run the pipeline.
5. Score with `task.utility(model_output_text, pre_env, post_env)`. There is also a trace-based check, `utility_from_traces`, but no default suite overrides it, so Banking is scored only on final state plus final answer text.

Two problems here:
- If the final output is `None`, it **re-runs the pipeline up to 3 times on the already-changed environment** (lines 383–394).
- `benchmark.py:run_task_without_injection_tasks` **silently maps provider errors to utility=False, security=True** (lines 284–312), which breaks our rule that errors are their own outcome.

**What AgentDojo records per run.** `logging.py:TraceLogger` writes one JSON file per (pipeline, suite, user_task, attack, injection_task). It holds the messages, an error string, utility, security, duration, a timestamp, `agentdojo_package_version` and `benchmark_version`. It records **no tokens, no per-call latency, no provider or model ID, and no cost**. Without a logdir it writes into `site-packages/../../runs`, i.e. inside `.venv`.

**Built-in defenses** (the `DEFENSES` list in `agent_pipeline.py:43`):
- `tool_filter` (OpenAI only)
- `transformers_pi_detector` (protectai deberta-v3; needs torch and transformers, which aren't installed)
- `spotlighting_with_delimiting`
- `repeat_user_prompt` (the sandwich defense)

Also available: the `PromptInjectionDetector` base class, which can abort a run via `AbortAgentError`, and the `planner.py` `ToolSelector`/`ToolUsagePlanner` stubs.

**Attacks.** These are milestone 2 work, flagged now so they don't surprise us later: `important_instructions` calls `get_model_name_from_pipeline`, which raises for model names not in `models.py:MODEL_NAMES`.

## 2. What we reuse and what we replace
- **Reuse:** `get_suite`, the task classes and their `utility()` predicates, `SystemMessage`, `InitQuery`, `ToolsExecutionLoop`, `ToolsExecutor`, `FunctionsRuntime`, and `GroundTruthPipeline` (for tests). That keeps scoring identical to AgentDojo's.
- **Replace:** the LLM element (our own `LabLLM`), the runner (one attempt, explicit outcomes, telemetry), and the logger/store.

## 3. Modules and files (milestone 1 only)
```
pyproject.toml            agentdojo==0.1.35 pinned; dev: ruff, mypy, pytest; script `lab = lab.cli:main`
.env.example, .gitignore  GROQ_API_KEY placeholder; ignore .env, results/*.sqlite, cache/
.github/workflows/ci.yml  lint, types, tests; no secrets, tests block network
DECISIONS.md
configs/experiments/smoke.yaml          3 Banking tasks, T1, FakeModel (CI); Ollama variant optional, local only
configs/experiments/baseline_banking.yaml  16 tasks, T1, Groq large + small, 3 repeats
configs/prices.yaml       public paid $/Mtok per model, with source URL and date
lab/config.py             pydantic ExperimentConfig, ModelSpec(provider, model_id, temperature, max_tokens)
lab/errors.py             InfrastructureError (never scored; runner re-queues)
lab/models/types.py       ModelRequest, ModelResponse(message, prompt_tokens, completion_tokens, latency_s, cached)
lab/models/client.py      ChatClient Protocol; OpenAICompatClient(base_url, api_key_env); FakeClient(scripted)
lab/models/cache.py       SQLite; key = sha256(provider, model_id, messages, tools, temperature, max_tokens)
lab/models/llm_element.py LabLLM(BasePipelineElement): ChatMessage<->OpenAI dicts, cache -> scheduler -> client,
                          appends a CallRecord to ctx; malformed tool JSON -> tool error the model sees, counted
lab/scheduler/limits.py   per-provider RPM/RPD/TPM limiter (injectable clock), backoff on 429/5xx, DailyCapReached
lab/topologies/base.py    Topology Protocol: build(llm: BasePipelineElement, system_message: str) -> BasePipelineElement
lab/topologies/t1_single.py  AgentDojo default shape, using LabLLM
lab/domains/agentdojo_bridge/banking.py   load_banking_suite(), banking_task_ids()
lab/domains/agentdojo_bridge/run_case.py  run_case(...) -> CaseResult (generic over AgentDojo suites)
lab/runner.py             expand config -> RunSpecs; skip specs already in store (resume); re-queue infra errors
lab/store.py              SQLite `runs` table (one row per run, trace as JSON)
lab/scoring/metrics.py    benign utility, Wilson CI, bootstrap-over-tasks CI, model-error breakdown, p50/p95, tokens, cost
lab/cli.py                `lab run <config>`, `lab report <experiment>`
tests/                    see Verification
```

**Interfaces**
- `run_case(suite, pipeline, user_task) -> CaseResult`. It mirrors `run_task_with_pipeline` but makes exactly one attempt, and scores with the task's own `utility_from_traces` / `utility` methods. The outcome is one of:
  - `solved`
  - `not_solved`
  - `max_iterations` (a model error)
  - `no_final_answer` (a model error)

  Model errors count as not solved. The raw state check is still stored so nothing is lost. `InfrastructureError` and any unexpected exception propagate: they are never turned into a score.
- `RunContext` is passed in `extra_args["ctx"]` and collects a `CallRecord` per call. The record holds model, provider, tokens, latency, whether it was a cache hit, and the malformed-tool-call count. It is added to `run_case` in the model-client slice.
- `RunRecord` (pydantic) holds: run_id, experiment, config_hash, git_commit, agentdojo version, benchmark version, provider, model_id, date, seed, repeat_idx, user_task_id, outcome, utility, the call records, the messages, total tokens, malformed tool calls, cost_usd and wall latency.

## 4. Decisions
1. **Thin OpenAI-compatible client, not LiteLLM.** We need exact control over temperature, the system role, the cache key, the retry policy and usage counts.
2. **SQLite for both the cache and the run store, in separate files.** It is in the standard library and suits row appends and resume lookups.
3. **Pin benchmark v1.2.2 with agentdojo==0.1.35.** It is the latest version installed and the CLI default, and it includes the Banking fixes.
4. **T1 is AgentDojo's own pipeline shape with our `LabLLM` swapped in. A topology is any `BasePipelineElement`.** T2–T5 must act on the same `env`, give workers a filtered `FunctionsRuntime`, and end with an assistant message holding the final answer. The defense hooks map onto existing seams:
   - `on_untrusted_input` → the ToolsExecutor output formatter
   - `on_tool_call` → a `FunctionsRuntime` subclass wrapping `run_function`
   - `on_handoff` → inside the orchestrator element

## 5. Open questions / risks
- **Model IDs** (check current Groq docs and tool-calling support before pinning): `llama-3.3-70b-versatile` (large) and `llama-3.1-8b-instant` (small).
- **Sample size.** Banking has only 16 user tasks, so a 95% CI on benign utility is about ±20 points even with repeats. The headline gate is on 11 tasks, where 3/11 has a Wilson CI of 9.7%–56.6%: fine as a "does it work at all" check, noisy for anything finer.
- **Smoke config.** The brief asks for 2 topologies; milestone 1 has only T1.

## 6. Build order (slices)
1. **Project skeleton + Banking bridge + `run_case` + ground-truth parity test** (16/16). CI.
2. **Model client** (OpenAI-compatible + FakeClient), `LabLLM`, cache.
3. **Scheduler** (limits, backoff, infrastructure errors), runner, store, resume. *Done 2026-10-25; layout in DECISIONS.md D22-D26.*
4. **Metrics, report, CLI, configs**, FakeModel smoke run in CI. *Done 2026-10-25; D32-D38. The baseline runs 1 repeat, not 3 (D36).*
5. **Real runs on Groq** (large + small), handoff.

## 7. Verification
- `uv run pytest`. None of these tests call a real model, and the tests block network sockets.
  - Ground-truth parity: `GroundTruthPipeline` through our `run_case` gives utility=True on all 16 Banking tasks.
  - FakeClient scripted runs give `solved` and `not_solved`; malformed tool JSON is returned to the model and counted; a 429 storm raises `InfrastructureError` and the run is re-queued, not scored.
  - Cache hit and miss, and a check that no secret ends up in the cache key.
  - Limiter tested with a fake clock.
  - Store resume skips completed runs.
  - Wilson CI matches known values.
- `uv run ruff check . && uv run mypy lab`
- FakeModel smoke run in CI. Optional local run: Ollama smoke, then `lab report smoke`.
- `lab run configs/experiments/baseline_banking.yaml` on Groq, with a second identical run showing 0 new calls (everything from the cache).

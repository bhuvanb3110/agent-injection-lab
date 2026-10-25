# Decisions

One entry per design decision: what was decided, why, and what was rejected. Newest at the bottom.

## Milestone 1

### D1. Thin OpenAI-compatible client instead of LiteLLM (2026-10-25)
- **Decision:** One small client built on the `openai` SDK with a configurable `base_url`. It covers Groq, Ollama, Cerebras, Gemini's OpenAI endpoint and GitHub Models.
- **Why:** We need exact control over the request, and a wrapper library makes it harder to show what was actually sent. Specifically:
  - temperature 0 must actually be sent
  - the system role
  - the cache key
  - one retry policy that we own and count
  - per-call tokens and latency

  The `openai` SDK is already pinned through agentdojo.
- **Rejected:**
  - *LiteLLM:* it adds its own retries, caching and model-name mapping, i.e. one more layer to audit.
  - *AgentDojo's `OpenAILLM`:* it drops `temperature=0` (`temperature or NOT_GIVEN`, `openai_llm.py:162`), sends the system prompt with role `developer`, crashes on malformed tool JSON, and retries invisibly with tenacity.

### D2. SQLite for the cache and the run store, in separate files (2026-10-25)
- **Decision:** Two SQLite files, one for the response cache and one for the run store.
- **Why:**
  - It is in the standard library.
  - It suits row-by-row appends and key lookups for resume.
  - It is crash-safe in WAL mode.
  - ~70k rows is small.

  Separate files mean the run store can be wiped without losing cached responses.
- **Rejected:** DuckDB. It is better for analytics, but that isn't the bottleneck here, and it can still read the SQLite files later.

### D3. Pin AgentDojo benchmark v1.2.2 and agentdojo==0.1.35 (2026-10-25)
- **Decision:** All Banking runs use `get_suite("v1.2.2", "banking")`, which has 16 user tasks and 9 injection tasks.
- **Why:** It is the latest benchmark version in the installed package and the default of `agentdojo/scripts/benchmark.py`. It includes the Banking fixes to UserTask15 (v1.1.1), the injection tasks (v1.2) and UserTask6 (v1.2.2).
- **Caveat:** The AgentDojo paper reports v1 numbers, so compare only against results on v1.2.2.

### D4. A topology is any AgentDojo `BasePipelineElement`; T1 is AgentDojo's default shape (2026-10-25)
- **Decision:** T1 = `[SystemMessage, InitQuery, llm, ToolsExecutionLoop([ToolsExecutor, llm])]`, with our own LLM element.
- **Why:** AgentDojo scores only the final environment and the last assistant message. So any topology that acts on the same `env` and ends with an assistant answer is scored identically. T2–T5 can then be custom elements without forking AgentDojo's scoring.

### D5. Two kinds of error: infrastructure errors are re-queued, model errors count as not solved (2026-10-25)
- **Decision:**
  - *Infrastructure errors* (429, 5xx, timeouts after our retries) are never scored. The run is re-queued and resumed later.
  - *Model errors* (`max_iterations`, `no_final_answer`) count as not solved and are reported as a breakdown next to benign utility.
- **Why:** An outage is not a property of the model and must not move utility. A model that loops or never answers did fail the task, but readers need to see *how* it failed.
- **Implementation:** `InfrastructureError` and any unexpected exception propagate out of `run_case`; they are never converted into a score. AgentDojo's `benchmark.py` does convert them, scoring them as utility=False, security=True, which is why we don't use it.
- **Note:** Malformed tool calls were listed as a model error in the original change request. Under D6 they no longer end a run, so they are a per-run count rather than an outcome: a run with malformed calls can still be solved.

### D6. Malformed tool-call JSON goes back to the model as a recoverable tool error (2026-10-25)
- **Decision:** When a model emits tool-call arguments that aren't valid JSON, it receives a tool error message and can retry. The run counts these per run, and nothing crashes. This is built in the model-client slice.
- **Why:** This matches how real tool-calling APIs behave and how `FunctionsRuntime.run_function` already handles bad arguments (as an error string). Crashing would turn a recoverable model mistake into a missing data point.

### D7. Each case runs exactly once (2026-10-25)
- **Decision:** `run_case` makes a single attempt. A final assistant message with `content=None` is recorded as `no_final_answer`.
- **Why:** AgentDojo's `run_task_with_pipeline` re-runs the pipeline up to 3 times on the already-changed environment when the output is `None` (`task_suite.py:383`). That hides failures, and a second attempt can complete a transfer the first one half-did.

### D8. CI smoke uses the FakeModel; the Ollama smoke run is local only (2026-10-25)
- **Decision:** The CI smoke test runs on scripted model responses. The Ollama smoke config is optional and run by hand.
- **Why:** CLAUDE.md says CI never calls a real model, and CI runners have no Ollama.

### D9. `run_case` keeps the raw state check alongside the outcome (2026-10-25)
- **Decision:** `CaseResult` stores both `utility` (True only when the outcome is `solved`) and `state_check_passed` (the task's `utility()` predicate, evaluated even for model-error outcomes).
- **Why:** Under D5, a run that hits `max_iterations` counts as not solved even if the transfer went through. Keeping the raw check lets the analysis show that case instead of losing it.

### D10. Tests block network sockets (2026-10-25)
- **Decision:** An autouse pytest fixture in `tests/conftest.py` makes any socket connect raise.
- **Why:** It enforces "tests never call a real model" mechanically, locally and in CI, without adding a dependency such as pytest-socket. CI also passes no secrets.

### D11. Three typed errors: InfraError, ModelError, ConfigError (2026-10-25)
- **Decision:** `lab/errors.py` defines three exceptions. The client raises them; nothing inside a run turns them into a score.
  - *InfraError:* 408, 409, 429, 5xx, timeouts and connection errors. Re-queued (D5). Carries the status code and the `Retry-After` value.
  - *ModelError:* any other 4xx (e.g. 400), or a response with no choices or a non-function tool call. Retrying the same request would fail the same way.
  - *ConfigError:* missing key, 401/403, 404, or a model not in `configs/providers.yaml`. Stops the experiment.
- **Why:** The transient set is the one the openai SDK itself retries (`openai/_base_client.py:_should_retry`), so the line between "try again later" and "this will never work" isn't ours to invent. ConfigError was added beyond the brief: without it, a bad key would show up as ModelError, and under D5 every run would be scored as not solved.
- **Rename:** PLAN_M1 called the first one `InfrastructureError`; it is now `InfraError`.
- **Open:** how the runner records ModelError (its own outcome or not solved) is decided in the scheduler slice.

### D12. The cache key is a hash of exactly what is sent (2026-10-25)
- **Decision:** `ModelRequest` holds provider, model ID, messages, tools, temperature, max_tokens and the repeat index. The client sends its fields; the cache key is sha256 of its canonical JSON (sorted keys, fixed separators).
- **Why:** If the key and the request are built from the same object, nothing that changes the response can be left out of the key. The API key lives only inside the SDK's HTTP client and is never a field of `ModelRequest`, so it can't reach the key, the cache file or a trace. Both are tested.
- **Not sent:** `tool_choice`. With tools present the OpenAI default is already "auto", and Ollama doesn't support the field.

### D13. Cache semantics (2026-10-25)
- **Decision:**
  - A hit is flagged `cached=True` and keeps the original call's latency, tokens and cost.
  - Only successful responses are stored; errors never are.
  - The repeat index is part of the key.
  - The canonical request is stored next to the response so any hit can be audited.
- **Why:** Re-running analysis must cost zero calls but still report what the original call cost. Without the repeat index, repeats 2 and 3 of a case would replay repeat 1's responses. That would hide run-to-run variance, which CLAUDE.md forbids.

### D14. Message conversion rules (2026-10-25)
- **Decision:** In `lab/models/llm_element.py`:
  - System prompt sent with role `system`.
  - All content sent as plain strings, text blocks only.
  - Tool results sent as `error or content`, the same rule as AgentDojo's `OpenAILLM`.
  - No `name` field on tool messages.
  - Reasoning text (Groq's `reasoning` field) stored in the `CallRecord` but never put into the transcript.
- **Why:**
  - Groq returns a 400 for `messages[].name` (https://console.groq.com/docs/openai), which AgentDojo's `OpenAILLM` sends.
  - Plain strings are the format every OpenAI-compatible server accepts.
  - Banking's `utility()` reads the final message text, so reasoning in the transcript could leak an answer into the score.

### D15. Malformed tool calls: `MalformedToolCall` + `LabToolsExecutor` (2026-10-25)
- **Decision:** Implements D6.
  - Tool-call arguments that aren't a JSON object (invalid JSON, or valid JSON such as `[]` or `null`) become a `MalformedToolCall`: a `FunctionCall` subclass with empty `args` that keeps the raw text and the parse error.
  - `LabToolsExecutor` (a `ToolsExecutor` subclass) answers it with a tool error saying the tool was not run. Valid calls in the same turn go one at a time to AgentDojo's own executor, in the model's order.
  - In the history the malformed call is replayed with arguments `{}`; the tool error quotes the original text.
  - The count is per call in `CallRecord.malformed_tool_calls` and summed per run.
- **Why:**
  - The stock `ToolsExecutor` can't tell a malformed call apart, and would run it with empty args. That actually executes zero-argument tools such as `get_balance`.
  - Replaying `{}` avoids a provider rejecting invalid JSON in the history.
  - The rule is strict and simple: nothing is "repaired".
- **Constraint:** Every topology that uses `LabLLM` must use `LabToolsExecutor`.

### D16. Groq Llama models are gone; the Groq model choice needs re-approval (2026-10-25)
- **Finding:** Groq shut down `llama-3.3-70b-versatile` and `llama-3.1-8b-instant` for free and developer tiers on 2026-08-16. The recommended replacements are `openai/gpt-oss-120b` and `openai/gpt-oss-20b` (https://console.groq.com/docs/deprecations, checked 2026-10-25).
- **Decision for now:** `configs/providers.yaml` lists only the gpt-oss pair for Groq, plus `qwen3:8b` for local Ollama. The model pair for the baseline is not final until Bhuvan approves it.
- **Caveats:**
  - Both are reasoning models. Groq suggests temperature 0.5–0.7 for reasoning models, and converts 0 to 1e-8.
  - Neither supports parallel tool calls on Groq.
  - The published limits (30 RPM, 1K RPD, 8K TPM, 200K TPD) may be the Developer plan's, not the Free plan's; this is marked `verified_for_free_plan: false`.

### D17. Per-run telemetry travels in `extra_args["ctx"]` (2026-10-25)
- **Decision:** `run_case` creates (or receives) a `RunContext` and passes it as `extra_args["ctx"]`. `LabLLM` appends one `CallRecord` per call and raises if no context is present. `CaseResult` now carries `calls` and `malformed_tool_calls`.
- **Why:** AgentDojo already threads `extra_args` through every element, including `ToolsExecutionLoop`, so no AgentDojo class needs changing. Refusing to call without a context means no call can go uncounted.

### D18. Approved model pair: gpt-oss-120b (large) and gpt-oss-20b (small) on Groq (2026-10-25)
- **Decision:** Bhuvan approved `openai/gpt-oss-120b` as the large model and `openai/gpt-oss-20b` as the small model. This settles the open part of D16.
- **Deviation from the brief:** RQ5 asked about "small (about 8B) vs large (70B+)" models. It now becomes **small vs large within one model family**: 20B vs 120B total parameters, both mixture-of-experts.
  - The small arm is larger than the brief's 8B, so a "small models are hijacked more" result will be weaker evidence about truly small models.
  - In exchange, the pair differs mainly in size: same family, same training recipe, same chat format. That makes the size comparison cleaner.
- **Why:** Groq retired the Llama pair from the free tier (D16), and the gpt-oss pair is Groq's recommended replacement. Both have tool calling, confirmed by the probe below.
- **Probe (2026-10-25):** one tiny tool-calling request per model (one `get_time(city)` tool, no Banking task). Temperature 0.6, `reasoning_effort=medium`, `max_tokens=512`.
  - Both models returned `finish_reason="tool_calls"`, `content=None`, and a standard `tool_calls[]` entry (`type="function"`, `function.arguments` as a JSON string). That is exactly what `OpenAICompatClient` parses. Tool-call IDs look like `fc_<uuid>`.
  - Reasoning came back as `message.reasoning`, which the SDK exposes in `model_extra` as D14 expects.
  - Usage: 120b had 148 input and 63 output tokens, 36 of them reasoning. 20b had 148 input and 37 output tokens, 14 of them reasoning. Reasoning is reported in `usage.completion_tokens_details.reasoning_tokens` and is part of `completion_tokens`.
  - Groq also returns `usage_breakdown` and `x_groq` fields. Neither is used.

### D19. Sampling: temperature 0.6 and reasoning_effort "medium", fixed per model and recorded per run (2026-10-25)
- **Decision:** Every run uses the per-model `sampling` from `configs/providers.yaml`: `temperature: 0.6` and `reasoning_effort: medium`. Both are part of the run spec, and therefore of the run ID and the stored record, and of the cache key. `reasoning_effort` is sent only when set, because Ollama doesn't take it.
- **Why temperature 0.6, not 0:** BRIEF §7 asks for "temperature 0 where the provider allows". For reasoning models Groq recommends 0.5–0.7, with a default of 0.6, "to prevent repetitions or incoherent outputs" (https://console.groq.com/docs/reasoning, checked 2026-10-25). It also silently turns 0 into 1e-8 (D16).
  - At 0.6, runs are not deterministic, so the 3 repeats measure real variance. They must be reported, never averaged away.
  - The model pages for these models give no temperature of their own.
- **Why "medium":** Groq documents low, medium and high for gpt-oss but states no default. Pinning a value means a silent change to the provider's default can't move our results. Medium is the middle setting.
  - Low would save only a few output tokens. Input tokens dominate the token budget (about 8K input per task, see D20).
  - Medium makes it more likely that the small model clears the ~20% utility gate.
  - This is an open question for Bhuvan.
- **Reasoning tokens:** they are counted. The probe shows `completion_tokens` already includes them, so `output_tokens` and cost (at the output price) include reasoning. `reasoning_tokens` is also stored on its own, per call and per run, so we can see how much of the output was reasoning.
- **Rejected:** temperature 0. It is against Groq's guidance for these models, and it isn't really sent anyway.

### D20. Free-tier limits come from the account's limits page; tokens/day is the binding limit (2026-10-25)
- **Decision:** `configs/providers.yaml` takes its limits from https://console.groq.com/settings/limits, read by Bhuvan on 2026-10-25. These replace the docs-page values marked unverified in D16. Limits are per model and not shared across models.

  | Model | RPM | RPD | TPM | TPD |
  | --- | --- | --- | --- | --- |
  | openai/gpt-oss-120b | 30 | 1K | 8K | 200K |
  | openai/gpt-oss-20b | 30 | 1K | 8K | 200K |
  | llama-prompt-guard-2-22m | 30 | 14.4K | 15K | 500K |
  | llama-prompt-guard-2-86m | 30 | 14.4K | 15K | 500K |

  The Prompt Guard models are listed now (with `tool_calling: false`) for the detection defenses in milestone 4.
- **Rate-limit headers (probe and https://console.groq.com/docs/rate-limits):**
  - Groq always sends `x-ratelimit-limit|remaining|reset-requests`, which track **requests per day**, and `x-ratelimit-limit|remaining|reset-tokens`, which track **tokens per minute**.
  - `retry-after` (in seconds) comes only with a 429.
  - There is no header for RPM or TPD, so the scheduler tracks those itself.
  - Reset values are durations such as `1m26.4s` and `3.165s`. 86.4 s is one day divided by 1000, which suggests requests replenish gradually rather than resetting at midnight.
  - Surprise: after the 211-token probe call, `remaining-tokens` fell by 422 (7578 of 8000). For 20b it was 185 tokens vs a drop of 373. Groq's TPM accounting seems to count about twice the reported usage, perhaps by reserving output tokens. The scheduler trusts the header whenever it has one.
- **Token budget per Banking task (estimate, no API calls):**
  - Calibrated from the probe: about 2.6 characters of request JSON per prompt token.
  - The AgentDojo default system prompt plus the 11 Banking tool schemas (5,250 characters of JSON) come to about **2.2K tokens on every call**.
  - Replaying each task's ground-truth trace gives 2–6 calls per task (mean 3.1) and **about 7.9K input tokens per task** (range 4.9K–17K). The largest single prompt is about 3.4K, well under the 8K TPM.
  - A real model takes more calls than the ground truth. Assuming about 1.5–2× as many calls, plus reasoning output, a realistic figure is **about 12–17K tokens per task**.
- **Tasks per day:** 200K TPD allows about **12–16 Banking tasks per model per day** at that rate, or 23–25 if the model takes the ground-truth path.
  - The baseline is 16 tasks × 3 repeats = 48 runs per model, which takes about **3–4 days**.
  - The two models have separate limits, so they progress side by side.
  - If Groq's TPD accounting also counts double (see the surprise above), that halves. This is why the scheduler must stop cleanly at the cap and resume later.

### D21. A ModelError is stored as outcome "model_error" with a subtype, and counts as not solved (2026-10-25)
- **Decision:** When a run raises `ModelError` (for example an HTTP 400, a response with no choices, or a non-function tool call), the scheduler stores the run with `outcome="model_error"`, `utility=False`, and a subtype such as `http_400`, `no_choices` or `unsupported_tool_call_type`.
  - The run counts in the denominator of headline utility, as not solved.
  - It is never re-queued and never dropped.
  - The calls made before the error are kept.
  - `max_iterations` and `no_final_answer` remain their own outcomes. They are also model errors under D5, so the report groups all three in its model-error breakdown.
- **Why:** This settles the open point in D11. Retrying the same request would fail the same way, so re-queueing would loop forever. Dropping the run would bias utility upward, which CLAUDE.md forbids.

### D22. Rate limiting: four sliding windows per model, plus the provider's headers (2026-10-25)
- **Decision:** `lab/scheduler/limits.py:RateLimiter`, one per model.
  - It logs every request it lets through as (time, tokens).
  - Before the next request it checks requests and tokens over the last 60 s against RPM and TPM, and over the last 24 h against RPD and TPD.
  - When the last response carried Groq's headers, it also obeys remaining tokens per minute and remaining requests per day (D20), and takes the stricter of the two counts.
- **Token estimate before sending:** request JSON length / 2.5, plus an output reserve. The reserve is `max_tokens`, or 1024 (Groq's default for reasoning models) if `max_tokens` isn't set. The probe measured 2.6 characters per token; rounding down to 2.5 makes the estimate err high.
  - After the call, the estimate is replaced by the reported input + output tokens.
  - A 429 counts 0 tokens, because Groq refused it before doing any work.
  - Other failures count the input estimate, because the prompt may have been processed.
- **Persistence:** every real request goes into the `requests` table of the run store. A new invocation seeds its limiter from the last 24 h of that table. Without this, a restart would forget the day's usage and blow through TPD.
- **Why sliding windows:** they are simple to explain and test, and never more permissive than a fixed "midnight" reset. Groq's own windows appear to refill gradually (D20), so a sliding window is the closer model.
- **Rejected:** trusting the headers alone. They cover only two of the four limits.

### D23. One retry policy, for InfraError only, applied per call (2026-10-25)
- **Decision:** `lab/scheduler/retry.py:ScheduledClient` wraps the provider client, inside the cache: `LabLLM -> CachingClient -> ScheduledClient -> OpenAICompatClient`. A cache hit therefore never waits for or uses up a limit.
  - It retries `InfraError` up to 5 attempts in total.
  - After failed attempt n, it waits a random time in [d/2, d], where d = min(60 s, 2 s × 2^(n−1)) ("equal jitter").
  - If the provider sent `Retry-After`, it waits at least that long.
  - `ModelError` and `ConfigError` are never retried.
- **Long waits:** when a 429 asks for a wait longer than `max_wait_s` (120 s), the client raises `DailyCapReached` instead of sleeping. This is how a tokens/day 429 from Groq ends up as a clean stop, even when our own TPD count missed it.
- **Why per call, not per run:** retrying the whole run would redo calls that already succeeded and spend tokens twice. A retried call sends the identical request.
- **Attempts:** each `CallRecord` stores its `attempts` (1 plus retries, 0 for a cache hit). The run's `requests_sent` is their sum, so retries are counted, never hidden.

### D24. Run ID = sha256 of the RunSpec; provenance is recorded but not hashed (2026-10-25)
- **Decision:** A `RunSpec` holds experiment, suite, benchmark version, user task, topology, provider, model ID, temperature, reasoning_effort, max_tokens and repeat index. The run ID is sha256 of its canonical JSON.
  - Re-running a spec is idempotent: a stored run ID is skipped.
  - Duplicate specs in one queue are run once.
- **Not in the hash:** git commit, agentdojo package version and date. They are stored on every run but kept out of the ID, so a new commit doesn't make every finished run look new.
  - The agentdojo version is pinned (D3), so it can't drift silently.
  - Anything that does change the task (benchmark version, sampling) *is* in the spec.
- **Seed:** stored as `null`, because we send no seed. Groq picks its own per request (the probe's `x_groq.seed` changed between calls). This is an open question for Bhuvan.

### D25. Run store: runs, calls and requests in one SQLite file, insert-only (2026-10-25)
- **Decision:** `lab/store.py:RunStore` has three tables:
  - `runs`: one row per scored run. It has every field in BRIEF §9: config hash, git commit, provider, model ID, date, seed, full trace, tokens, cost, latency, outcome, failure label. It also has the agentdojo version, benchmark version, temperature, reasoning_effort, max_tokens, repeat index, outcome subtype, state check, call and cache counts, reasoning tokens and malformed tool calls.
  - `calls`: one row per model call, as flat columns plus the full `CallRecord` JSON.
  - `requests`: every real provider request, including failed attempts.
- **Insert only:** a duplicate run ID raises an error, because rerunning a stored run is a bug.
- **Totals:** a run's token or cost total is `null` if any call lacks that number. It is never guessed.
- **Failed runs:** a run that ended in InfraError or a daily cap has no `runs` row. It was never scored. Its requests are still in `requests`, so the cost of failures stays visible.
- **The trace:** messages are stored as JSON, with `FunctionCall` objects dumped by pydantic. A `model_error` run has no transcript, because AgentDojo's pipeline raises before returning messages. Its calls are kept.

### D26. The scheduler queue: sequential, per-model stop, bounded re-queue (2026-10-25)
- **Decision:** `lab/scheduler/queue.py:Scheduler.run(specs)` runs each pending spec through an injected `run_fn(spec, ctx)` and decides by how the run ended:
  - **A `CaseResult`:** stored.
  - **`ModelError`:** stored as `model_error` (D21).
  - **`InfraError` after retries:** not stored. The run goes to the back of the queue, at most `max_requeues` (1) times per invocation, then stays pending for a later invocation. This stops an outage from turning into an endless loop.
  - **`DailyCapReached`:** the run is abandoned unscored, and runs for *that model* are skipped for the rest of the invocation. Other models carry on, since limits are per model. A later invocation restarts the abandoned run from scratch, with a fresh environment. Its earlier calls then come from the cache.
  - **`ConfigError` or any other exception:** raised, which stops the experiment.
- **Why sequential:** one run at a time keeps the limiter, the store and the trace order simple. The cost is idle time while one model is throttled. Running models in parallel can come later if wall time becomes the bottleneck.
- **Deviation from PLAN_M1 §3:** the planned `lab/runner.py` and `lab/scheduler/limits.py` became `lab/scheduler/{clock,limits,retry,spec,queue}.py`. Expanding an experiment config into RunSpecs moves to slice 4, together with the config loader and the CLI.

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

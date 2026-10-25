# Agent Injection Lab — Project Brief

Oct 25, 2026 · Bhuvan

## How to use this brief

Everything the building assistant needs is here.

Instructions for the assistant that builds this project:

1. Read the whole brief before writing code. The research questions (section 2) decide what gets built; the code serves the measurements.
2. Keep the budget at $0. Use only official free API tiers and local models (section 8). Never use scraped, reverse-engineered or shared API keys.
3. Build one milestone at a time (section 10). Each milestone ends with working code, passing tests and a short note in `DECISIONS.md`.
4. Prefer a small, finished, measured system over a large, half-built one. Propose scope cuts (section 13) before adding features.
5. Never weaken the measurement to make results look better: no tuning defenses on the test split, no dropping failed runs, no hiding variance.
6. Attacks only ever target the project's own sandboxed agents and mock tools. No real email accounts, websites, payment systems or third-party services (section 12).
7. Explain design choices so the builder can defend them in an interview. The builder must understand every line.

## 1. About the builder

The builder is Bhuvan Bethapudi, an aspiring SWE / applied AI engineer targeting frontier labs (Anthropic, OpenAI) and top AI startups, including the Anthropic Fellows Program (AI Security workstream).

- **Prior project: rote** ([github.com/bhuvanb3110/rote](https://github.com/bhuvanb3110/rote)). An AI discovers a task on legacy banking UIs once and records it as a typed artifact; a deterministic Playwright engine replays it with zero model calls. It has a shared safety gate, human escalation, confidence gating from locator drift, a capability catalog (`GET /capabilities`, JSON Schema, `POST /capabilities/:id/invoke`), multi-tenant overrides proven against a label-free DOM, record-time redaction of sensitive fields, and 83 tests.
- **Strengths to build on:** safety thinking for irreversible actions, verifying against ground truth instead of trusting summaries, honest limits sections.
- **Budget:** $0. Free API tiers, local models, free GPU notebooks only.
- **Story this project should tell:** "I build agents for high-stakes settings, and I measure where they break and how to make them safe."

## 2. Project summary and research questions

**Agent Injection Lab** measures whether more layers of agent oversight make tool-using agents safer against prompt injection, or just add more places for attacks to enter. It runs the same business tasks, with attacks hidden in the content agents read, across several agent topologies and defenses, and reports attack success, task success and cost side by side.

It is a research harness first and a framework second: the framework exists so the findings are reproducible and other people can plug in their own agents and domains.

| ID | Research question | Starting hypothesis |
| --- | --- | --- |
| RQ1 | How does attack success rate change across topologies: single agent, orchestrator + workers, + LLM reviewer, + deterministic checks, + human gate? | An LLM reviewer helps against obvious attacks but misses attacks that arrive inside retrieved content. |
| RQ2 | Does an injection that enters one worker spread to the planner or other workers through handoffs and summaries? | Summaries pass injected instructions upward; multi-agent setups can be worse than a single agent. |
| RQ3 | Which entry channel is hardest to defend: user message, retrieved documents (RAG), emails, web pages, tool outputs, or tool descriptions? | Retrieved documents and tool outputs beat defenses most often. |
| RQ4 | For each defense, how much attack success does it remove, how much benign task success does it cost, and at what latency and token cost? | Architectural defenses (permissions, isolation) beat prompt-based ones at a lower utility cost. |
| RQ5 | Do small (about 8B) and large (70B+) models differ in how often they are hijacked and how useful they are as reviewers? | Small models are hijacked more; reviewers matter more for them. |
| RQ6 (stretch) | Can a small fine-tuned injection classifier match an LLM judge at a fraction of the cost, and does it survive adaptive attacks? | It matches on known attacks and degrades on adaptive ones. |

The main headline result is a **security–utility–cost frontier**: for each topology and defense, where it sits on attack success vs. task success vs. cost.

## 3. Threat model and prior work

The gap this project fills: strong single-agent injection benchmarks exist, but there is little careful, reproducible measurement of how **multi-agent topologies** change attack success, how injections **spread between agents**, and what each defense costs in utility and money on **small open models**.

### Threat model

- **Attacker controls:** some content the agent reads — a retrieved document, an email, a web page, a tool's output, or a tool's description. Prompt injection is still #1 on the [OWASP Top 10 for LLM Applications 2026](https://www.invicti.com/blog/web-security/owasp-llm-top-10-2026-whats-new), and Excessive Agency rose from #6 to #3.
- **Attacker does not control:** the user's request, the system prompts, the code, or the model weights.
- **Attacker goals:** (1) unauthorized action, such as a money transfer; (2) data exfiltration; (3) sabotage of the user's task; (4) persistence in memory or files; (5) propagation to other agents or outputs.
- **Assets at risk:** account balances, private documents, credentials in context, the integrity of final outputs.
- **Guiding principles to test against:** the "lethal trifecta" (private data + untrusted content + external communication) and Meta's Rule of Two, which says an agent should hold at most two of: untrusted input, sensitive access, external state changes, or else require human approval ([Zylos summary](https://zylos.ai/research/2026-04-12-indirect-prompt-injection-defenses-agents-untrusted-content/)).

### Prior work to build on and cite

| Work | What it is | How this project uses it |
| --- | --- | --- |
| [AgentDojo](https://arxiv.org/html/2406.13352v1) (ETH Zurich, NeurIPS 2024) | Extensible environment: 97 user tasks, 27 injection tasks, 629 security cases across Workspace, Slack, Travel and Banking suites. Metrics: benign utility, utility under attack, targeted ASR. Repo: github.com/ethz-spylab/agentdojo | Reuse its environments, tasks and metrics; add multi-agent pipelines. Its Banking suite connects directly to rote. |
| [Prompt Infection](https://pith.science/paper/2410.07283) (Lee and Tiwari, 2024) | Shows injections can replicate between agents like a virus; proposes "LLM tagging" as a defense. Reviewers flag missing quantitative comparisons. | Replicate under controlled conditions and give the missing tagged-vs-untagged numbers (RQ2). |
| [Self-replicating prompt injections](https://alignment.openai.com/misalignment-reports/self-replicating-prompt-injections-exist/) (OpenAI, disclosed Sept 2026) | Worm-like injections that reach a goal and get the agent to republish themselves, spreading via email, files and code comments. | Include self-replication as an attacker goal; measure spread across agents. |
| [Design patterns for securing LLM agents](https://simonwillison.net/2025/Jun/13/prompt-injection-design-patterns/) (Beurer-Kellner et al., 2025) | Six patterns: Action-Selector, Plan-Then-Execute, LLM Map-Reduce, Dual LLM, Code-Then-Execute, Context-Minimization. | Implement 2–3 of them as architectural defenses (section 6). |
| CaMeL (Google DeepMind) | Dual-LLM design with capability-based policies; solved 77% of AgentDojo tasks with provable security vs. 84% undefended ([Zylos](https://zylos.ai/research/2026-04-12-indirect-prompt-injection-defenses-agents-untrusted-content/)). | Reference point for the utility cost of strong architectural defenses. |
| "The Attacker Moves Second" (Nov 2025) | Adaptive attacks bypassed 12 published defenses at over 90% success ([Zylos](https://zylos.ai/research/2026-04-12-indirect-prompt-injection-defenses-agents-untrusted-content/)). | Every defense must also face an adaptive attack, not just the fixed suite. |
| PoisonedRAG (USENIX Security 2025) | Poisoned knowledge-base entries, reported 97% attack success against RAG. | Model for the RAG entry channel. |
| MCP tool poisoning and "rug pulls" | Hidden instructions in tool descriptions, or descriptions that change after approval. | Tool-description channel (RQ3). |
| [Inspect](https://inspect.aisi.org.uk) (UK AI Security Institute) | Open eval framework: tasks, solvers, scorers, agents, sandboxes, a log viewer, multi-agent primitives. | Optional: package the final evals as Inspect tasks so others can run them. |

**Positioning sentence for the write-up:** "AgentDojo measures single agents; this project extends it to multi-agent topologies on free, small models and reports the security–utility–cost frontier, including propagation between agents and adaptive attacks."

## 4. Architecture

The harness keeps everything domain-specific in a domain pack and everything else generic, so any topology and defense stack can run against any domain's tasks and attacks.

A config picks the domain, topology, defense stack and models; the injector plants one payload; every model call passes through the defense middleware; the scorer judges success from the environment's final state and the trace.

### Topologies under test

| Topology | Shape | Why it is included |
| --- | --- | --- |
| T1 Single agent | One agent with all tools | Baseline; matches AgentDojo's default |
| T2 Orchestrator + workers | Planner splits the task; 2–3 workers each run a subtask with a subset of tools | The common multi-agent pattern; tests propagation through summaries |
| T3 T2 + LLM reviewer | A reviewer agent approves each worker's result or proposed action | Does LLM oversight catch injections? |
| T4 T2 + deterministic checks | Policy engine and state checks instead of an LLM reviewer | LLM oversight vs. rule-based oversight |
| T5 T3 + human gate | Simulated human approval before irreversible actions | The strongest oversight; tests approval manipulation |

### Domain packs

A domain pack provides four things: tasks (user requests with expected outcomes), tools (mock functions over an environment state), a checker (deterministic predicates on final state), and a policy (rules such as amount caps).

1. **AgentDojo Banking** — first and main domain; ties directly to rote.
2. **AgentDojo Workspace or Slack** — second domain; email and messaging channels.
3. **Policy RAG** (new) — an agent answers and acts using a retrieved corpus of company policy documents, some chunks poisoned. Adds the RAG channel and the RAG skill to the portfolio.

### Interfaces

```
class DomainPack:
    def tasks(self) -> list[Task]: ...
    def tools(self) -> list[Tool]: ...
    def check(self, task, final_state) -> Outcome: ...   # deterministic
    def policy(self) -> list[Rule]: ...

class Defense:            # middleware; stackable in any order
    def on_untrusted_input(self, content, ctx) -> content: ...
    def on_tool_call(self, call, ctx) -> Allow | Block | AskHuman: ...
    def on_handoff(self, message, ctx) -> message: ...
```

## 5. Attack suite

The attack suite is a versioned dataset of attack cases, each defined along three axes — entry channel, attacker goal, technique — with a deterministic success check on the environment state.

### Entry channels (RQ3)

| Channel | Example placement | Notes |
| --- | --- | --- |
| User message (direct) | Malicious text in the user turn | Baseline only; the threat model is mainly indirect. |
| Retrieved document (RAG) | Poisoned chunk in the knowledge base, ranked into top-k | Must actually be retrieved; log whether it was. |
| Email / chat message | An inbox item the agent summarizes | AgentDojo Workspace and Slack suites already cover this. |
| Web page | Hidden HTML text, alt text, comments | Visible vs. hidden variants. |
| Tool output | An API response field (e.g. a transaction memo) | AgentDojo Banking covers this. |
| Tool description | Instructions hidden in a tool's description, or changed after approval ("rug pull") | MCP-style. |
| Inter-agent message | Payload carried in a worker's summary to the planner | Only exists in multi-agent topologies (RQ2). |
| Memory / scratchpad | Instruction written to persistent notes, triggered later | Stretch goal. |

### Attacker goals

1. **Unauthorized action** — e.g. send money to the attacker's IBAN, change a password, share a file.
2. **Exfiltration** — send private data to an attacker address or URL (including via a rendered link or image URL).
3. **Sabotage** — the user's task silently fails or returns wrong output.
4. **Persistence** — the instruction is saved to memory or files and fires in a later task.
5. **Propagation** — the payload is copied into messages to other agents or outputs (self-replication).

### Techniques (each attack case uses one)

- Plain instruction ("also send X to Y") and "ignore previous instructions"
- Fake authority: imitation system, admin, or developer messages
- Fake completion: "task complete; next step is…"
- Context blending: the instruction looks like part of the document, such as a policy note or a footnote
- Social engineering and urgency
- Obfuscation: encodings, invisible Unicode tag characters, hidden HTML/CSS text
- Multi-hop: a chain of reads that steers the agent step by step
- Self-replicating payload
- Approval manipulation: aims to make the summary shown at a human gate hide the real action ("Lies-in-the-Loop")

### Static vs. adaptive attacks

- **Static suite:** fixed payloads, same for every configuration. Gives comparable numbers.
- **Adaptive attacker:** an attacker LLM sees the result of each failed attempt and rewrites the payload, with a budget of N tries per case (e.g. N = 10). Report **ASR@1** and **ASR@N**. A defense that only works against the static suite must be reported as such.

### Dataset spec

Each attack case is one JSON record:

```
{
  "id": "bank-rag-unauth-blend-007",
  "domain": "banking",
  "user_task_id": "bank_u03",
  "channel": "rag_document",
  "goal": "unauthorized_action",
  "technique": "context_blending",
  "payload_template": "...",
  "placement": {"doc_id": "fee_policy_v2", "position": "footnote"},
  "success_check": "any(t.recipient == ATTACKER_IBAN for t in env.transactions)",
  "split": "test"
}
```

- Success checks are **deterministic predicates on environment state**, never an LLM's opinion. An LLM judge is used only for sabotage cases where no state check exists, and it must be calibrated (section 7).
- **Dev / test split:** defenses are developed on dev cases only; final numbers come from the held-out test split.
- Size target: AgentDojo's 629 security cases plus about 150–300 new cases for the new channels, goals and techniques.

## 6. Defenses

Defenses are composable middleware with one interface, so any topology can be run with any combination. Four families are compared, from cheapest and weakest to most structural.

| Defense | Family | Where it acts | What it should stop | Expected cost |
| --- | --- | --- | --- | --- |
| Delimiters / spotlighting / datamarking | Prompt | Wraps untrusted content | Naive instructions | Near zero; probabilistic |
| Sandwich (repeat user task after content) | Prompt | Prompt assembly | Task drift | Extra tokens |
| Off-the-shelf injection classifier (an open model from Hugging Face) | Detection | On each untrusted input | Known phrasings | One small model call per input |
| LLM judge / alignment check ("does this action serve the user's request?") | Detection | Before each tool call | Off-task actions | One LLM call per action |
| Own fine-tuned small classifier (RQ6) | Detection | Same as classifier | Known + in-domain attacks | Training once, then cheap |
| Least-privilege tool sets per agent | Architecture | Worker tool lists | Actions the worker never needs | Some utility loss |
| Quarantined reader (Dual LLM) | Architecture | Untrusted content is read by an agent with no tools that returns structured fields only | Instruction-following from content | Extra call; schema design |
| Plan-then-execute | Architecture | Plan fixed before reading content | Content changing which actions run | Lower flexibility |
| Provenance tags on inter-agent messages | Architecture | Every handoff | Propagation (RQ2) | Tokens |
| Policy engine for irreversible actions (recipient allowlist, amount caps) | Deterministic | Tool-call boundary | Unauthorized transfers regardless of model | Rules to write per domain |
| Egress allowlist (no unknown URLs, no external images) | Deterministic | Output and network | Exfiltration | Near zero |
| Human approval gate showing the **raw tool call**, not an LLM summary | Human | Before irreversible actions | Everything that reaches it, including approval manipulation | Human time; simulated in experiments |

Rules for implementing defenses:

- Each defense must name the attacker goal it targets, so a failure is interpretable.
- The human gate is simulated by a deterministic "reviewer" that approves exactly what a careful human would approve given the raw call; separately report the case where it sees only an LLM-written summary (this mirrors rote's lesson: verify against ground truth, not summaries).
- Combinations to test at minimum: none; prompt only; detection only; architecture only; deterministic only; architecture + deterministic; everything.
- Heavier published defenses (CaMeL, MELON, FIDES) are cited as reference points, not reimplemented, unless time remains.

## 7. Metrics and experiment design

Every configuration is scored on security, utility and cost together, on the same attack cases, so differences between configurations are paired comparisons.

### Metrics

| Metric | Definition | Answers |
| --- | --- | --- |
| Benign utility | Share of user tasks solved with no attack present | Does the defense break normal work? |
| Utility under attack | Share of attack cases where the user task is still solved and no attacker effect occurs | Overall usefulness under attack |
| Targeted ASR | Share of attack cases where the attacker's success check passes | Core security number |
| ASR@N (adaptive) | Share of cases broken within N adaptive attempts | Robustness, not just luck |
| Propagation rate | Share of runs where the payload appears in another agent's input or a final output; plus infection depth (hops) | RQ2 |
| Detector precision / recall / false-positive rate | For classifier and judge defenses, on attack and benign content | Do detectors block real work? |
| Retrieval hit rate | Share of RAG attack cases where the poisoned chunk was actually retrieved | Separates retrieval from obedience |
| Tokens, calls, cost per task | Counted per run; cost at public paid prices even though runs are free | Cost axis of the frontier |
| Latency p50 / p95 | Wall-clock per task | Production viability |

### Experiment matrix

- **Factors:** topology (5) × defense combination (about 7) × model size (2) × domain (2–3).
- The full grid is too large for free tiers, so run it in two stages:
  1. **Screening:** two sweeps on a stratified 10% sample of cases (about 85, balanced across channel, goal and technique), not a full factorial grid — a topology sweep and a defense sweep (sizes in section 8).
  2. **Confirmation:** the full test split only for the most informative configurations (the frontier points and the surprises).
- **Repeats:** temperature 0 where the provider allows; re-run a 10% subset three times to measure nondeterminism and report it.
- **Failure taxonomy:** every failed or hijacked run gets one label — wrong plan, context lost at handoff, wrong tool use, reviewer approved bad action, policy violation, retrieval failure, injection obeyed, injection propagated. Label automatically where possible, hand-check a sample.

### Statistics

- Report every rate with a 95% confidence interval (Wilson interval for proportions; bootstrap over tasks for utility).
- Compare configurations on the same cases with McNemar's test, and report effect sizes, not only p-values.
- Write the hypotheses and analysis plan into `PREREGISTRATION.md` before touching the test split.
- LLM judges: hand-label 50–100 examples, report agreement with the judge (Cohen's kappa), and only use the judge if agreement is acceptable.
- Never drop failed or errored runs silently; errors are reported as their own category.

## 8. Free stack and cost plan

The whole project runs at $0 on official free API tiers, a local model and free notebook GPUs; the engineering that makes this work (rate-limit-aware scheduling, caching, resume) is itself a portfolio feature.

### Models and providers

Free-tier limits change often; check each provider's current docs before relying on these figures ([OpenRouter comparison, Apr 2026](https://openrouter.ai/blog/tutorials/free-llm-apis-compared/); [Paterson snapshot, May 2026](https://ianlpaterson.com/blog/free-llm-api-2026/)).

| Role | Provider | Free limit (approx.) | Notes |
| --- | --- | --- | --- |
| Large model (70B-class) | Groq | ~30 req/min, ~1,000 req/day per model | Fast, OpenAI-compatible, no card |
| Large model, bulk runs | Cerebras | ~1M tokens/day | Catalog has shrunk without notice; pin the model |
| Small model (8B-class) | Groq (llama-3.1-8b-instant) | ~14,400 req/day | Main workhorse for the small-model arm |
| Small model, unlimited | Ollama on the builder's machine | Hardware-bound | Simulated users, attacker drafts, debugging |
| Overflow / variety | Gemini (AI Studio), GitHub Models | Per-model daily caps | Gemini free prompts may be used for training outside EU/UK; data here is synthetic, so acceptable |
| Fine-tuning (RQ6) | Google Colab / Kaggle free GPUs | Session and weekly quotas | A small encoder classifier trains in minutes |

The models used must support tool calling; check this per model before choosing.

### Run budget

| Sweep | Runs | Approx. LLM calls (×10 per run) |
| --- | --- | --- |
| Benign utility baseline | 5 topologies × 2 models × 97 tasks = 970 | ~10k |
| Topology sweep | 5 topologies × no defense × 2 models × 85 cases = 850 | ~8.5k |
| Defense sweep | 7 combinations × 2 topologies × 2 models × 85 cases = 2,380 | ~24k |
| Adaptive attacks | Best 3 configs × 30 cases × up to 10 attempts | ~9k |
| Confirmation on the full test split | Chosen configs only | ~10–20k |

Total is roughly 60–70k calls, spread across providers over 2–3 weeks of overnight runs. If limits tighten, cut the sample to 50 cases before cutting a research question.

### Engineering that keeps it free

- **One model interface** (LiteLLM or a thin OpenAI-compatible client); the config picks the provider per role.
- **Scheduler** with per-provider limits (per minute and per day), retries with backoff, and **resume** after the daily cap.
- **Cache** keyed by model + provider + full prompt + tools + temperature; re-running analysis costs zero calls.
- **Pin provider and model version per experiment.** Never mix providers inside one comparison: free endpoints may serve different quantizations, which would confound results. Log provider, model ID and date on every run.
- **Cost column at public paid prices**, computed from token counts, so the cost axis is real even though nothing was paid.

## 9. Repo structure and engineering standards

Python, with AgentDojo as a dependency for environments and scoring; everything the project adds lives in its own package so it can later be contributed upstream or wrapped as Inspect tasks.

```
agent-injection-lab/
  README.md                 # case study: question, method, results, limits
  PREREGISTRATION.md        # hypotheses + analysis plan, committed before test runs
  DECISIONS.md              # one entry per design decision and why
  configs/
    topologies/*.yaml       # single, orchestrator_workers, +reviewer, ...
    defenses/*.yaml         # composable defense stacks
    experiments/*.yaml      # sweep definitions (factors, sample, models)
  lab/
    models/                 # provider-agnostic client, rate limits, cache
    scheduler/              # queue, retries, resume, daily caps
    topologies/             # agent pipelines plugged into AgentDojo
    defenses/               # middleware: prompt, detection, architecture, policy
    attacks/
      static/               # payload templates + generator
      adaptive/             # attacker-LLM loop, ASR@N
    domains/
      agentdojo_bridge/     # wraps Banking, Workspace, Slack, Travel suites
      rag_docs/             # new RAG channel: corpus + poisoned chunks
      tool_desc/            # MCP-style tool-description poisoning
    tracing/                # every message, call, handoff, tokens, cost
    scoring/                # metrics, propagation detection, failure labels
    analysis/               # CIs, McNemar, frontier plots
  data/
    attack_cases/           # versioned JSONL, dev/test split
    judge_labels/           # hand labels for judge calibration
  results/                  # run store (SQLite or DuckDB) + generated reports
  report/                   # static HTML dashboard
  tests/
  .github/workflows/ci.yml
```

Standards:

- Python 3.11+, `uv` for environments, `pydantic` for configs and records, `ruff` + `mypy`, `pytest`.
- **CI never calls a real model.** Tests use recorded responses from the cache and a fake model, so CI is free and deterministic.
- A one-command smoke run: `lab run configs/experiments/smoke.yaml` (3 cases, 2 topologies, local model).
- Every run record stores: config hash, git commit, provider, model ID, date, seed, full trace, tokens, cost estimate, latency, outcome, failure label.
- Results are only ever produced by scripts from the run store, never edited by hand.
- Dashboard: a static HTML report (frontier plot, per-channel and per-technique ASR, propagation graph, failure breakdown, example traces) that can be hosted for free on GitHub Pages.

## 10. Milestones

Core results land in about eight weeks and the write-up is public by early January; the fine-tuned classifier is a stretch goal after publishing.

Dates assume roughly 15–20 hours a week; shift the whole plan if the start date moves. Each milestone has an exit criterion that must be true before the next starts:

1. **Setup and baseline** — AgentDojo Banking runs end to end with T1 on one free large model and one small model through the scheduler and cache. Exit: benign utility measured for both; small model clears about 20% or is swapped.
2. **Topologies + tracing** — T1–T5 implemented as pipelines; tracer records every message and handoff; scorer computes utility, ASR and propagation. Exit: one AgentDojo attack runs through all five topologies with full traces.
3. **Attack suite v1** — the Policy RAG domain, new channels and techniques, dataset in JSONL with a dev/test split, judge calibration labels. Exit: `PREREGISTRATION.md` committed (gate).
4. **Defenses + sweeps** — defense middleware built on dev cases; topology sweep and defense sweep run on the screening sample. Exit: first frontier plot.
5. **Adaptive attacks** — attacker-LLM loop against the best three configurations. Exit: ASR@1 and ASR@N reported.
6. **Confirm + analysis** — full test split for chosen configurations; confidence intervals, McNemar tests, failure taxonomy. Exit: every number in the write-up regenerates from one script.
7. **Write-up + release** — README case study, report, dataset with datasheet, dashboard, video. Exit: published (gate).
8. **Stretch: RQ6** — fine-tune a small injection classifier on free GPUs; compare with the LLM judge and test against adaptive attacks.

## 11. Deliverables

The project is done when a stranger can read one write-up, see the frontier chart, and re-run the headline result with one command.

1. **Public repo** with the README written as a case study: question, setup, results with confidence intervals, limits, how to reproduce.
2. **Write-up** (blog post or short arXiv-style report, 6–10 pages): the security–utility–cost frontier, the propagation finding, the adaptive-attack results, and what surprised you.
3. **Released attack dataset** on Hugging Face or GitHub, with a datasheet (what it contains, intended use, limits).
4. **Static dashboard** on GitHub Pages: the frontier plot, ASR by channel and technique, a propagation graph, and clickable example traces.
5. **Two-minute video walkthrough** for reviewers who will not clone the repo.
6. **Optional upstream contribution:** a pull request adding multi-agent pipelines or the new channels to AgentDojo, or packaging the evals for Inspect.

Resume bullets to fill in with real numbers once results exist (do not use these placeholder shapes before measuring):

- Built an open harness measuring prompt-injection risk across 5 multi-agent topologies and 7 defense stacks on AgentDojo plus N new attack cases; ran ~N k evaluations at $0 on free-tier and local models.
- Found that [finding, e.g. LLM reviewer agents cut direct-injection ASR by X% but only Y% for injections arriving via retrieved documents]; architectural defenses reached Z% ASR at W% utility cost.
- Measured injection propagation between agents (X% of hijacked workers infected the planner); provenance tagging reduced spread to Y%.
- Fine-tuned a small classifier that matched an LLM judge at X% recall with 1/Y the cost; adaptive attacks reduced recall to Z%.

## 12. Ethics, safety and responsible disclosure

All attacks run only against the project's own sandboxed agents and mock tools; the project studies defenses, and its release is shaped so it helps defenders more than attackers.

- **Sandbox only.** No real inboxes, websites, bank accounts, payment APIs or third-party services are ever targeted. Mock tools only; no network egress from agent tools.
- **No attacks on provider safety systems.** The goal is injection through content an agent reads, not jailbreaking the providers' models for harmful content. Stay within each provider's terms of use.
- **Release with care.** Release the attack templates and benchmark cases (as AgentDojo and similar benchmarks do), but not a tuned "universal" payload optimized against a specific named product.
- **Responsible disclosure.** If testing a real product or open-source agent framework reveals a vulnerability, report it privately to the maintainer first and publish only after a fix or a reasonable window (commonly 90 days).
- **Honest framing.** Report limits clearly: simulated environments, free-tier models, simulated human gate. Do not claim any defense makes agents "secure".

## 13. Risks, pitfalls and scope cuts

The biggest risk is not a technical failure but an unfinished project; cut scope in the order below before letting the timeline slip.

| Risk | Early sign | Response |
| --- | --- | --- |
| Small models are too weak to do the benign tasks | Benign utility under ~20% for the 8B model | ASR is meaningless if the agent can't act. Report ASR only on tasks the model solves without attack, or swap to a stronger small model. |
| Free tiers tighten or a model disappears | 429s, removed models | Pin a fallback per role; shrink the sample to 50 cases; lean on the local model. |
| Tool calling breaks on free endpoints | Malformed tool calls, provider errors | Count as its own error category; never as "attack failed". |
| AgentDojo integration takes too long | Not running one suite end to end by day 4 | Time-box to 4 days, then fall back to a minimal own banking environment with the same metrics. |
| Defenses overfit to the attacks | Big dev-to-test gap | Strict dev/test split; adaptive attacks on final configs. |
| LLM reviewer shares the worker's blind spots | Reviewer approves the same hijacked actions | This is a finding, not a bug; also test a reviewer from a different model family. |
| No significant differences | Overlapping confidence intervals | A careful null result is still publishable; report it with intervals and power. |
| Scope creep | New features before milestone 3 is done | Follow the cut order below. |

Cut order if time runs short (first to cut at the top):

1. Memory / persistence channel
2. RQ6 fine-tuned classifier (can become its own follow-up project)
3. Third domain
4. Tool-description (MCP) channel
5. Adaptive attack budget reduced from N = 10 to N = 3

Never cut: the benign utility baseline, the dev/test split, confidence intervals, the propagation measurement (RQ2), or the write-up.

## Sources

- [AgentDojo: A Dynamic Environment to Evaluate Prompt Injection Attacks and Defenses for LLM Agents (arXiv)](https://arxiv.org/html/2406.13352v1)
- [Indirect Prompt Injection: Attacks, Defenses, and the 2026 State of the Art — Zylos Research](https://zylos.ai/research/2026-04-12-indirect-prompt-injection-defenses-agents-untrusted-content/)
- [Prompt Infection: LLM-to-LLM Prompt Injection within Multi-agent Systems — Pith review](https://pith.science/paper/2410.07283)
- [Self-replicating prompt injections exist — OpenAI Alignment](https://alignment.openai.com/misalignment-reports/self-replicating-prompt-injections-exist/)
- [Design patterns for securing LLM agents against prompt injections — Simon Willison](https://simonwillison.net/2025/Jun/13/prompt-injection-design-patterns/)
- [OWASP LLM Top 10 2026: what's new — Invicti](https://www.invicti.com/blog/web-security/owasp-llm-top-10-2026-whats-new)
- [Inspect — UK AI Security Institute](https://inspect.aisi.org.uk)
- [Free LLM API in 2026: 13 Options Ranked and Compared — OpenRouter](https://openrouter.ai/blog/tutorials/free-llm-apis-compared/)
- [Free LLM API Tiers in 2026 — Ian Paterson](https://ianlpaterson.com/blog/free-llm-api-2026/)

# Agent Injection Lab

A case study in progress: do more layers of agent oversight make tool-using agents safer against prompt injection, or do they just add more places for attacks to enter?

The lab runs the same banking tasks with and without hidden instructions in the content the agents read. It does this across several agent topologies, with small open models on free API tiers ($0 budget), and reports attack success, task success and cost side by side. **There are no attack results yet.** This README covers the question, the design, what is built and the baseline measured so far.

- Full spec: [docs/BRIEF.md](docs/BRIEF.md)
- Milestone 2 plan: [docs/PLAN_M2.md](docs/PLAN_M2.md)
- Every design decision and its reasons: [DECISIONS.md](DECISIONS.md)

## The question

Does attack success change with the agent topology (RQ1)? Does an injection that enters one worker spread to the planner or to other workers through handoffs and summaries (RQ2)? The starting hypothesis is that summaries pass injected instructions upward, so a multi-agent setup can be worse than a single agent. The other research questions (entry channels, defense cost, small versus large models) are in the brief.

## Threat model

- The attacker controls some content an agent reads: here, a file or the subject of an incoming transaction in the mock bank.
- The attacker does not control the user's request, the system prompts, the code or the model weights.
- The attacker's goal is an unauthorized action, such as redirecting a payment to the attacker's IBAN.
- Everything is sandboxed. The tools are AgentDojo's mock Banking tools, and nothing touches a real account, inbox or website.

## Setup

- **Benchmark:** AgentDojo, Banking suite only (16 user tasks, 9 injection tasks). Pinned to `agentdojo==0.1.35`, benchmark v1.2.2 (D3).
- **Models:** `gpt-oss-120b` (large) and `gpt-oss-20b` (small) on Groq's free tier, temperature 0.6 (D18, D19).
- **Attack:** AgentDojo's `important_instructions` template, plus a canary line so spread can be traced (D50).

## Topologies

| Name | Shape |
| --- | --- |
| **T1** | One agent with all the tools. |
| **T2** | A planner with no Banking tools delegates to two workers: payments (7 tools) and account (4 tools). The planner sees only what a worker reports back. |
| **T3** | T2 plus an LLM reviewer that reads each worker's report before the planner does, and each write call before it runs. |
| **T4** | T2 plus a deterministic policy check on write calls. Recipients are trusted only if they come from structured fields, never from free text. |
| **T5** | T3 plus a simulated human who sees the raw call and approves it only if it matches the user's intent. It is an oracle, so an upper bound. |
| **T5-summary** | T3 plus the same human, who sees only a description of the action written by the worker that proposed it. |

Each oversight mechanism is implemented once and attached at a named checkpoint, so a topology effect and a defense effect can't be confused (PLAN_M2 §3).

## What is built

- A thin OpenAI-compatible model client with exact request control, a response cache and a rate limiter for the free tier.
- A resumable run store (SQLite) and scheduler. Every run records its versions, seed, provider, model ID and date.
- The six topologies above, built from one table of presets.
- Tracing: every agent's messages, handoffs and gate decisions, and exact-match markers that follow an injection hop by hop (`lab trace`).
- Metrics for attack success, utility under attack, propagation and depth, gate decisions, and a per-run breakdown of runs that are safe but unsolved.
- FakeModels and scripted agents, so the whole system is tested without a real model.

## Milestone 1 results: the benign baseline (T1, no attack)

Utility is the share of tasks the agent solves, with 95% Wilson confidence intervals. Repeat 0 is 16 tasks per model. The pooled numbers add repeats 1 and 2, of which 90 of the 96 planned runs are stored so far.

- **A do-nothing agent scores 5/16 (31.25%).** Five Banking tasks pass without any action (D37). So the headline uses the 11 *discriminating* tasks, where doing nothing scores 0, and shows the all-task number beside it.
- **Repeat 0, discriminating tasks:** 20b solves 9/11 (81.8%, CI 52.3%–94.9%) and 120b solves 8/11 (72.7%, CI 43.4%–90.3%). On all 16 tasks: 14/16 and 12/16.
- **Pooled, discriminating tasks:** 120b 21/33 (63.6%, CI 46.6%–77.8%), 20b 20/28 (71.4%, CI 52.9%–84.7%).
- The two models can't be told apart: the intervals overlap almost entirely (D41).
- **Run-to-run variance is being measured.** At temperature 0.6, 7 of 16 tasks changed outcome across repeats for each model. A single run per task can't separate a topology effect from sampling noise, so the M2 exit check uses 3 repeats (PLAN_M2 §10).

## Current status

Milestone 1 (baseline) is done. Milestone 2 (topologies and tracing) is built and tested on FakeModels. The remaining steps are the real exit run on Groq (one attack case through all six topologies, 3 repeats, both models), which has not been run, then hand-checking the T5-summary summaries and writing the handoff. Milestones 3 and 4 (attack library, sweeps and defenses) are not started.

## Run the smoke tests

No API key or network is needed.

```
uv sync
uv run pytest
uv run ruff check . && uv run mypy lab
uv run lab run configs/experiments/smoke.yaml
uv run lab run configs/experiments/smoke_topologies.yaml
uv run lab report smoke_topologies
uv run lab trace <run_id or unique prefix>
```

The smoke runs use scripted FakeModels, so their numbers check the plumbing and say nothing about real models. Real runs need a Groq key in `.env` (see `.env.example`) and are resumable.

## Limits

- Banking only, one benchmark, two models from one family on one provider.
- Small samples. The M2 exit run is 1 attack case × 3 repeats, labelled a plumbing check, not a result. Sample sizes for later milestones will be set from measured cost only.
- Temperature 0.6 makes single runs noisy, and Groq's seed is best effort.
- AgentDojo has known quirks. `user_task_9` rewards inaction, and injection tasks 0, 1 and 3 count any transfer to the attacker IBAN as success. Both are flagged in reports and scored as AgentDojo does.
- The simulated human is deterministic. T5 is an upper bound, and T5-summary's parser is frozen at v1 with documented limits (D71).
- The canary line and a generic model name mean absolute attack success may differ slightly from AgentDojo's published numbers. Comparisons between our own configurations are unaffected (D47).
- A "question to the user" detector (`ended_with_question`) is a lower bound: it missed 2 of the 3 hand-labelled baseline cases (D73).

# Agent Injection Lab

Research harness measuring whether layers of multi-agent oversight make tool-using agents safer against prompt injection, or just add more places for attacks to enter. The full spec is `docs/BRIEF.md`. Read the sections relevant to the current task before writing code.

**Current milestone: 4** (BRIEF §10; milestones 1–3 are done: milestone 2 in `docs/PLAN_M2.md`, decisions D51–D89; milestone 3 in `docs/PLAN_M3.md`, decisions D90–D118, gated by `PREREGISTRATION.md`). Scope for milestones 1–4: AgentDojo **Banking** suite only.

## How we work

- One milestone at a time. A milestone is done only when it has working code, passing tests, entries in `DECISIONS.md`, and a handoff summary (format below).
- For any non-trivial change, plan first and wait for approval before writing code.
- Explain design choices. The builder (Bhuvan) must understand and be able to defend every line in an interview. Prefer simple, readable code over clever code.
- Propose scope cuts (BRIEF §13) before adding features. Don't build ahead of the current milestone.
- Commit in small, logical steps with clear messages.
- Read the DECISIONS.md index first; open full entries only for decisions relevant to the current task.

## Hard rules

- **$0 budget.** Official free API tiers and local Ollama only. Never use scraped, shared or reverse-engineered keys.
- **Secrets** live in `.env` (gitignored). Never print, log, commit or echo keys. Never include them in cache keys, traces or run records.
- **Tests and CI never call a real model.** Use the fake model and recorded responses.
- **Never weaken the measurement.** No tuning on the test split, no dropping failed or errored runs (errors are their own outcome category), no hiding variance.
- **Attacks only target this project's sandboxed agents and mock tools.** No real inboxes, websites, payment systems or third-party services.
- **Don't guess third-party APIs** (AgentDojo, Groq, LiteLLM, Ollama). Read the installed source in `.venv` or the official docs, and name the file or function you relied on.
- **Pin and record versions**: agentdojo package version, AgentDojo benchmark version, provider, model ID, date. Every run record stores them.

## Stack

Python 3.11+, `uv`, `pydantic`, `ruff`, `mypy`, `pytest`. Package name `lab`; CLI entry point `lab`.

## Commands

Keep this list current as commands are added.

- `uv run pytest` (no network; includes the FakeModel smoke run, `tests/test_smoke.py`)
- `uv run ruff check . && uv run mypy lab`
- `uv run lab run configs/experiments/smoke.yaml` (FakeModels, no key needed)
- `uv run lab run configs/experiments/baseline_banking.yaml` (real Groq calls; resumable: rerun the same command after a daily cap)
- `uv run lab run configs/experiments/baseline_banking_repeats.yaml` (repeats 1–2 of the baseline; real Groq calls; resumable)
- `uv run lab run configs/experiments/smoke_topologies.yaml` (FakeModels, T1 to T5-summary, benign + attack; also run in CI by `tests/test_smoke_topologies.py`)
- `uv run lab run configs/experiments/m2_exit_v2.yaml` (M2 exit check: real Groq calls, both models, 3 repeats; resumable; run only when Bhuvan says so. Superseded by `m2_exit_v3.yaml` (D82). Supersedes `m2_exit.yaml`, which stopped at 41/72 before D76, D78)
- `uv run lab run configs/experiments/m2_exit_v3.yaml` (M2 exit check, third attempt: v2 finished 72/72 but 3 runs ended on Groq `tool_use_failed`, D82. Same cells as v2; real Groq calls; resumable; run only when Bhuvan says so. Supersedes `m2_exit_v2.yaml`; don't pool them)
- `uv run python -m lab.attacks.dataset banking [--check]` (writes the attack dataset, split and subsets in `data/attack_cases/` from `banking_composition.yaml`; `--check` regenerates and compares bytes, no model calls; D112)
- `configs/experiments/step8_20b_j.yaml`, `step8_20b_c.yaml`, `step8_120b.yaml` (M3 step-8 dev runs, D105, D113–D115: case lists, dev cases only, run with `uv run lab run configs/experiments/<name>.yaml`; the runner refuses any case outside the dev split and places every listed case before the first call. Real Groq calls, resumable; run only when Bhuvan says so, in the order J, C, 120b. D114's two defects are fixed by D115)
- `uv run lab budget [--model <provider/model_id or model_id>]` (per model: tokens and requests used in the last 24 h and left, and when the next 50,000 tokens free up; reads the run store, makes no model calls)
- `uv run lab trace <run_id or unique prefix>` (one run's multi-agent trace: every agent, handoff and marker hop)
- `uv run lab report <experiment> [<experiment> ...] [--blind]` (e.g. `smoke`, `baseline_banking`; several are pooled and per-task flips across repeats are shown, e.g. `baseline_banking baseline_banking_repeats`)
- `uv run lab validate-rules [--store results/runs.sqlite] [--root <dir>]` (group A: pins, dataset, split, regeneration, golden fixtures; no store, run in CI. With `--store`, group B against the run store, local only. Exit 1 on any failure; D102, D117). `lab run` runs group A first for any experiment with test-split cases (and for pinned benign controls) and refuses it if A fails, so test cases stay refused until step 11 writes PREREGISTRATION.md. `lab run` also refuses test-confirm cases until the freeze log (a hash chain, `FREEZE_LOG.yaml`) holds a valid M6 confirmation entry. `lab report` hides test-screen and test-confirm outcomes until the matching unblinding entry; `--blind` hides every outcome. `prereg_sha256` is recorded on runs, never in the run ID. File formats: `docs/PREREG_FORMAT.md`
- `uv run lab derive-attacker-effect <experiment>` (fills `attacker_effect` for attack runs stored before final environments were kept, from their executed writes; marked `derived_from_trace`; no model calls; D86)
- `uv run lab label configs/labels/<experiment>.yaml` (writes hand failure labels into the store)
- `configs/experiments/m4_topology_20b.yaml`, `m4_benign_20b.yaml`, `m4_topology_120b.yaml`, `m4_benign_120b.yaml`, then `m4_topology_20b_repeats.yaml` and `m4_topology_120b_repeats.yaml` (M4 sweep, PREREGISTRATION §4; real Groq calls; resumable; run only when Bhuvan says so, in that order. Test-screen cases only in the topology sweep, and their outcomes stay blind under `lab report --blind` until unblinded. Run as `uv run lab run configs/experiments/<name>.yaml`)
- Options: `run` takes `--store` (default `results/runs.sqlite`), `--cache` (default `cache/responses.sqlite`), `--env-file` (default `.env`); `report` takes `--store`, `--config` (single experiment only), `--blind`; `label` and `trace` take `--store`

## Preregistration rules

- `PREREGISTRATION.md` and every file in its pin block are frozen. Changing one is an amendment: a dated entry in `DECISIONS.md`, a commit, and only then a run. Never edit a pinned file in place to make a check pass.
- **Operating rule:** after an amendment is committed, rerun `lab run` (or the guard check) before the next run starts (PREREGISTRATION §12, B4). An amendment that changes what runs must not be followed by a run on the old state.
- `FREEZE_LOG.yaml` and `PREREGISTRATION.md` history is never rewritten or force-pushed. Their git history is the record. Fix a mistake with a new commit, never with a rebase, amend or force push.
- Run `uv run lab validate-rules` (group A must pass 12 of 12) before any commit that touches configs, data or the preregistration.

## Handoff summary

When asked to "write the handoff", produce this, under 400 words, no code:

1. Milestone and status (done / partial / blocked)
2. What was built (one line per module)
3. Decisions made (one line each, with the reason)
4. Tests: count and pass/fail; ruff and mypy status
5. Results, if any: model, provider, model ID, n, benign utility with 95% CI, errors, calls, tokens, estimated cost, p50/p95 latency
6. Problems, surprises, and any deviation from the brief
7. Open questions for Bhuvan
8. Latest git commit hash

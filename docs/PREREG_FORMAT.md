# File formats for the preregistration (PLAN_M3 step 10, D117)

What step 11 must write so that `lab validate-rules` can read it. The code that reads these files is `lab/rules/prereg.py`; the checks are in `lab/rules/checks.py` (group A) and `lab/rules/store_checks.py` (group B). Nothing here writes `PREREGISTRATION.md`.

There are three files, all at the repository root or under `configs/`:

| File | Written by | Changes after the gate? |
|---|---|---|
| `PREREGISTRATION.md` | step 11 | only by a dated amendment, appended (D106). Its SHA-256 is `prereg_sha256` on every M4 run. |
| `FREEZE_LOG.yaml` | M4 (the six freezes), the unblinding step, the judge step | yes: records are added. It is separate from the prereg so that a freeze does not change the prereg's hash. |
| `configs/golden/behaviour_v1.yaml` | step 10 (already written) | no: it is pinned. |

## 1. `PREREGISTRATION.md`: two fenced blocks

A fenced block opens with three backticks at the start of a line, then its info string; it closes with three backticks alone on a line. Each of the two blocks below must appear exactly once. Everything else in the file is prose.

### The pin block (info string `yaml pin-block`)

A YAML mapping from a repository-relative path (forward slashes, no `..`) to the lowercase hex SHA-256 of the file's **raw bytes**. Raw bytes, so a changed line ending is a changed file; `.gitattributes` keeps every text file LF.

````
```yaml pin-block
data/attack_cases/banking_v1.jsonl: 0123...(64 hex characters)
data/attack_cases/banking_split.json: ...
configs/golden/behaviour_v1.yaml: ...
configs/policies/banking_v1.yaml: ...
```
````

What group A requires of it:

- **A1:** every path exists and its hash matches. A tampered or missing file fails.
- **A2:** every file under `data/attack_cases/` (any depth) is pinned, and `configs/golden/behaviour_v1.yaml` is pinned. An unpinned data file fails.
- Pin whatever else G6 freezes (PLAN_M3 §1): `configs/policies/banking_v1.yaml`, `configs/humans/summary_parser_v1.yaml`, `configs/humans/intent_overrides.yaml`, `configs/labels/m2_exit_summaries.yaml` (D88's hand verdicts), and the M4 topology-sweep configs under `configs/experiments/`. Group A does not insist on these, but an unpinned file is a file the prereg does not freeze.
- **Do not pin `configs/providers.yaml`.** Its limits and prices are re-checked before each real run. Its sampling fields are compared by A8 instead (below).
- A benign-control config that is pinned under `configs/experiments/` is a *registered experiment*: `lab run` applies the guard to it and stamps its runs with `prereg_sha256` (see section 4).

### The values block (info string `yaml prereg-values`)

The values the prereg states that live in a file that cannot be pinned whole. Today only the sampling settings, for **every model that has a `sampling` entry in `configs/providers.yaml`** (A8 compares both ways: a model in one and not the other fails).

````
```yaml prereg-values
sampling:
  groq/openai/gpt-oss-120b: {temperature: 0.6, reasoning_effort: medium}
  groq/openai/gpt-oss-20b: {temperature: 0.6, reasoning_effort: medium}
```
````

The key is `<provider>/<model id>`. `reasoning_effort` is `low`, `medium`, `high` or `null`.

## 2. `FREEZE_LOG.yaml`

A YAML mapping with three optional keys. An absent file means nothing is frozen yet, which group A accepts. Times are full UTC date-times with a `Z` (`2026-11-25T14:03:00Z`); a bare date is refused, because an order of events on one day has to be checkable.

```yaml
freezes:                       # one record per defense, six in all
  - defense: prompt            # prompt | detection | architecture | deterministic | arch_det | everything
    config: configs/defenses/prompt.yaml
    sha256: <64 hex>           # raw bytes of that file, at the time of the freeze
    frozen_at: 2026-11-20T09:00:00Z
  # ... architecture, detection, arch_det, everything
  - defense: deterministic     # = T4 = policy_v1 (D108): its record points at the policy file
    config: configs/policies/banking_v1.yaml
    sha256: <64 hex>
    frozen_at: 2026-11-20T09:05:00Z

unblinding:                    # the dated record that lets test-screen outcomes be read (D103)
  recorded_at: 2026-12-01T10:00:00Z
  note: all six defenses frozen; core results may be read

judge:                         # the judge's registration (D105); added after the gate
  registered_at: 2026-12-02T10:00:00Z
  code_sha256: <64 hex>
  labels_sha256: <64 hex>
  kappa: 0.71
```

Rules (group A, "A2 freeze log"):

- A freeze is valid if its defense appears once, its `config` file exists, and the file still has the recorded `sha256`. A config **edited after its freeze** fails. A re-freeze is an amendment, which this log does not take.
- The unblinding record is valid only if **all six** defenses have a valid freeze **at or before** its `recorded_at`. A premature record fails group A, and `lab report` and `lab trace` keep the outcomes hidden (D103).
- The judge's `kappa` must be at least 0.6.

**Defense configs and the first test-screen run (A2 "defense configs").** A config under `configs/defenses/` is a draft until an experiment uses it. An experiment in `configs/experiments/` uses a defense config if its YAML contains that config's path (`configs/defenses/...`) anywhere, in whatever field M4 gives it. If such an experiment has test-split cases, that config must be in the freeze log with the hash it has now, or group A fails, and with it the guard. An experiment on dev cases only may use a draft.

## 3. `configs/golden/behaviour_v1.yaml`

Frozen behaviour fixtures, run by A7 (`lab/rules/golden.py` documents the layout): `normalise`, `ended_with_question`, `requested_user_input`, `attacker_effect`, `summary_parser_v1` (and the digest of its file), and the sabotage constant with `lab_sabotage_0`. The same file holds the numbers group B re-checks: D74's confusion table and D88's agreement.

A fixture is a rule's frozen answer, taken from the rule's DECISIONS entry. A rule that changes its answer fails A7; code that is refactored without a change of behaviour does not (D102).

## 4. Group B and the run store

`lab validate-rules --store results/runs.sqlite` adds these checks (D102):

| Check | What it reads |
|---|---|
| B1 D74 confusion table | the stored runs of `baseline_banking` and `baseline_banking_repeats` that carry a hand annotation (`lab label`) |
| B2 D88 | `configs/labels/m2_exit_summaries.yaml` (no store needed) |
| B3 no test run before the prereg commit | `runs.started_at` against the commit that first added `PREREGISTRATION.md` (`git log --diff-filter=A`). A test run with no such commit fails. |
| B4 every M4 run carries the current prereg hash | `runs.prereg_sha256` of every test-split run and every benign run of a pinned experiment, against the SHA-256 of `PREREGISTRATION.md` today. **An amendment changes that hash**, so runs made before it fail this check unless they are re-made under the amended prereg (they have another run ID, so they are separate runs). |
| B5 judge | the table `judge_verdicts(run_id, scored_at, ...)`, which the judge module (step 7) must create with at least those two columns, against `judge.registered_at` and `kappa` |
| B6 blinding | the table `outcome_views`, written by `lab report` and `lab trace` whenever they show a test-screen run's outcome (`viewed_at`, `command`, `detail`, `test_screen_runs`), against `unblinding.recorded_at` |

A run is a **test run** if its case is in the test split (a run of milestone 2's attack is the AgentDojo case of its pair). **Test-screen** runs are the 50 cases of `banking_test_screen.json`.

## 5. `lab run`, `prereg_sha256` and `lab report --blind`

- `lab run` refuses an experiment that contains a test-split case, or that is a pinned benign-control config, unless **group A passes**. An absent `PREREGISTRATION.md` fails group A. Every other experiment, every dev-case experiment included, runs as before.
- A run that passes the guard has `prereg_sha256` set (SHA-256 of `PREREGISTRATION.md`'s raw bytes) in its run spec, and so in its run ID. A run made outside the guard has none, and its run ID is what it was (D48 pattern).
- `lab report --blind` shows per model and topology only the number of runs, model errors, calls, tokens, cost and latency. Without `--blind`, the same view replaces the outcomes of **test-screen** runs until `blinding_state` says the freeze log holds a valid unblinding record; other runs are reported in full. `lab trace` refuses a blinded run.

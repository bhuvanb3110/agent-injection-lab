"""`lab validate-rules`, the `lab run` guard and the blinding rules (PLAN_M3 §5, D102, D103, D116).

- `prereg`: the machine-readable parts of PREREGISTRATION.md (the pin block) and the freeze log.
- `scope`: which attack cases, and which split, an experiment or a stored run belongs to.
- `golden`: the frozen behaviour fixtures, run against the code.
- `checks`: group A (no store, runs in CI).
- `store_checks`: group B (reads the run store, local only).
- `guard`: what `lab run` refuses, and the blinding state `lab report` and `lab trace` obey.

The file formats are written down in docs/PREREG_FORMAT.md.
"""

from pathlib import Path

# The repository root: where PREREGISTRATION.md, FREEZE_LOG.yaml, configs/ and data/ live.
PROJECT_ROOT = Path(__file__).resolve().parents[2]

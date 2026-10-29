"""KEL-15 evaluation harness.

The harness is deliberately cheap and deterministic offline: recorded provider
transcripts replay through the real chat, kernel, approval, and recovery code,
while retrieval tasks call the real ``docs.answer`` handler over local fixtures.
No task reaches the network unless the operator explicitly passes ``--live``.

Public entry point::

    from evals import run_suite

    outcome = run_suite(out_dir="var/evals")
    raise SystemExit(outcome.exit_code)
"""

from .dataset import Dataset, DatasetError, load_dataset
from .runner import SuiteOutcome, run_suite

__all__ = ["Dataset", "DatasetError", "SuiteOutcome", "load_dataset", "run_suite"]

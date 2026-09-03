"""
test_golden_output — the plain-text output a pipe, a script or an agent sees must not move.

The visual work is for a human at a TTY. Everyone else — CI, `| tee`, a coding agent reading
stdout — must get byte-identical output to before. A promise like that is only worth something if
it is checkable, so `scripts/golden_output.py` records stdout, stderr AND the exit code for a set
of offline commands under NO_COLOR, and this test diffs them.

When a diff is intentional, review it by eye and re-record:
    python3 scripts/golden_output.py --record
"""
import pytest
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


# The corpora capture argparse output, and argparse's own wording is Python-version dependent:
# 3.10 renamed the "optional arguments:" section header to "options:". A corpus recorded on 3.12
# therefore fails on 3.9 for a reason that has nothing to do with this project — the CLI itself
# works fine there. Two spurious failures that a contributor cannot tell apart from a real
# regression is worse than no check, so the comparison is pinned to the version that recorded it.
#
# This is NOT a way to dodge a failure: the corpora exist to prove OUR output is stable, and one
# canonical interpreter is the only way to make that a meaningful claim. Logic regressions are
# still caught on every version in the matrix by the rest of the suite.
CORPUS_PYTHON = (3, 12)
_wrong_python = pytest.mark.skipif(
    sys.version_info[:2] != CORPUS_PYTHON,
    reason=f"corpus is recorded on Python {CORPUS_PYTHON[0]}.{CORPUS_PYTHON[1]}; argparse "
           f"section headers differ across versions (3.10 renamed 'optional arguments:' to "
           f"'options:'), so a cross-version diff reports a difference this project did not make")

@_wrong_python
def test_plain_output_matches_the_golden_corpus():
    r = subprocess.run([sys.executable, str(REPO / "scripts" / "golden_output.py"), "--check"],
                       capture_output=True, text=True, cwd=str(REPO), timeout=600)
    assert r.returncode == 0, r.stdout + r.stderr

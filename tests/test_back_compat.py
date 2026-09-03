"""
test_back_compat.py — the pre-1.1 command surface is a promise, so pytest enforces it.

1.1.2 makes `target` the primary noun and demotes `adapter` / `app` / `keys` to a compatibility
shim. Customers are mid-engagement on 1.1.1 driving the old forms from shell scripts and from
Claude Code, so "nothing breaks" has to be mechanically checked rather than asserted in a
changelog.

The corpus lives in tests/backcompat/ and is produced by scripts/back_compat.py. It records
stdout and the exit code only -- stderr is excluded on purpose, because the deprecation pointer
1.1.2 adds is written there. See that script's docstring for the full rationale, including which
surface is deliberately left out.
"""
import pytest
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CHECKER = REPO / "scripts" / "back_compat.py"


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
def test_legacy_command_forms_are_unchanged():
    """Every pre-1.1 invocation still prints exactly what it printed in 1.1.1.

    If this fails, do NOT re-record the corpus to make it pass. Either the change is a real
    regression for someone running the old commands, or it is intended -- and if it is intended
    it belongs in the changelog with the mapping spelled out, not silently absorbed.
    """
    r = subprocess.run([sys.executable, str(CHECKER), "--check"],
                       capture_output=True, text=True, cwd=str(REPO), timeout=600)
    assert r.returncode == 0, (
        "the pre-1.1 command surface drifted:\n"
        f"{r.stdout}\n{r.stderr}")


def test_corpus_is_present_and_substantive():
    """A gate whose corpus quietly emptied would pass while checking nothing."""
    corpus = sorted((REPO / "tests" / "backcompat").glob("*.txt"))
    assert len(corpus) >= 30, f"expected the full legacy surface, found {len(corpus)} case(s)"
    # The two intentional error cases are tiny; everything else must carry real help text.
    thin = [p.name for p in corpus
            if p.stat().st_size < 200 and not p.name.startswith(("unknown_", "adapter_unknown_"))
            and p.name != "adapter_build_bad_out.txt"]
    assert not thin, f"these baselines look empty rather than recorded: {thin}"

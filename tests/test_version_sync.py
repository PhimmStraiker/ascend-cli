"""
test_version_sync — VERSION drift guard.

The CLI's runtime VERSION constant and the pyproject.toml version are two hand-maintained copies.
`doctor` compares the runtime VERSION against the latest published release, so a release commit that
bumps one and forgets the other would make every user's doctor mis-report. This pins them equal, so
that mistake fails CI instead of shipping.
"""
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
for p in ("control", "runtime", "shells/cli"):
    sys.path.insert(0, str(REPO / p))
import ascend  # noqa: E402


def test_runtime_version_matches_pyproject():
    txt = (REPO / "pyproject.toml").read_text()
    m = re.search(r'^version\s*=\s*"([^"]+)"', txt, re.M)
    assert m, "pyproject.toml has no version line"
    assert ascend.VERSION == m.group(1), (
        f"VERSION={ascend.VERSION!r} != pyproject {m.group(1)!r} — bump both in the release commit")

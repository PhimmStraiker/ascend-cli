"""
test_fixtures_are_tracked.py — a test fixture that is not committed is a test that only I can run.

Two tests shipped in 1.1.2 referencing `tests/fixtures/*.har` files that were **never
committed**. `.gitignore` carries a deliberate `*.har` rule — a real HAR carries credentials, and
this repo goes public, so the rule is right — and it silently swallowed the fixtures. `git add -A`
reported nothing. The tests passed on the machine that created them, where the files sat
untracked in the working tree, and failed for everyone else with FileNotFoundError.

That is the same defect class as the pty tests fixed earlier in this release: green for the
author, red for every contributor, and indistinguishable from a real regression when someone
else hits it first. The fix is a narrow `!tests/fixtures/*.har` exception; this file is the guard
that keeps the exception honest in both directions.

It asserts two things that must both hold:

  1. every fixture the suite reads is TRACKED, not merely present on disk;
  2. no fixture carries a credential — because the moment we exempt a file type from the
     credential-protecting ignore rule, the protection has to be re-established here instead.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "tests" / "fixtures"


def _tracked(path: Path) -> bool:
    r = subprocess.run(["git", "ls-files", "--error-unmatch", str(path.relative_to(REPO))],
                       cwd=REPO, capture_output=True, text=True)
    return r.returncode == 0


def _fixture_files():
    return sorted(p for p in FIXTURES.glob("*") if p.is_file()) if FIXTURES.is_dir() else []


class TestEveryFixtureIsCommitted:
    def test_the_fixtures_directory_exists(self):
        assert FIXTURES.is_dir(), "tests/fixtures/ is missing — the suite reads from it"

    @pytest.mark.parametrize("name", ["ack_poll.har", "text_stream.har"])
    def test_the_named_fixtures_are_present_and_tracked(self, name):
        p = FIXTURES / name
        assert p.is_file(), f"{name} is missing"
        assert _tracked(p), (
            f"{name} exists on disk but is NOT tracked by git — it will vanish for everyone "
            f"else and the test that reads it will fail with FileNotFoundError")

    def test_no_fixture_is_untracked(self):
        untracked = [p.name for p in _fixture_files() if not _tracked(p)]
        assert not untracked, f"untracked fixtures: {untracked}"

    def test_every_fixture_a_test_reads_actually_exists(self):
        """Catches the inverse: a test referencing a fixture nobody added."""
        referenced = set()
        for t in (REPO / "tests").glob("test_*.py"):
            for m in re.finditer(r'"fixtures"\s*/\s*"([^"]+)"', t.read_text()):
                referenced.add(m.group(1))
            for m in re.finditer(r'fixtures/([\w.\-]+)', t.read_text()):
                referenced.add(m.group(1))
        missing = sorted(n for n in referenced if not (FIXTURES / n).is_file())
        assert not missing, f"tests reference fixtures that do not exist: {missing}"


class TestFixturesCarryNoCredentials:
    """`*.har` is gitignored precisely because HARs leak. Exempting these re-opens that door."""

    PATTERNS = {
        "anthropic key": r"sk-ant-[A-Za-z0-9_\-]{8,}",
        "straiker PAT": r"s6r_(?:pat|live)_[A-Za-z0-9]{8,}",
        "bridge key": r"\btc-[A-Za-z0-9]{12,}",
        "aws key id": r"\bAKIA[0-9A-Z]{12,}",
        "bearer header": r"[Bb]earer\s+[A-Za-z0-9._\-]{16,}",
        "authorization hdr": r'"name"\s*:\s*"[Aa]uthorization"',
        "set-cookie": r'"name"\s*:\s*"[Ss]et-[Cc]ookie"',
    }

    @pytest.mark.parametrize("name", ["ack_poll.har", "text_stream.har"])
    def test_no_credential_patterns(self, name):
        text = (FIXTURES / name).read_text()
        hits = {k: len(re.findall(v, text)) for k, v in self.PATTERNS.items()}
        found = {k: n for k, n in hits.items() if n}
        assert not found, f"{name} contains credential-shaped content: {found}"

    @pytest.mark.parametrize("name", ["ack_poll.har", "text_stream.har"])
    def test_no_real_host_survived_sanitisation(self, name):
        """A loopback port or a customer host in a committed fixture is a leak of a different kind."""
        d = json.loads((FIXTURES / name).read_text())
        hosts = {re.sub(r"^(https?://[^/]+).*", r"\1", e["request"]["url"])
                 for e in d["log"]["entries"]}
        bad = [h for h in hosts if "example.com" not in h]
        assert not bad, f"{name} still points at a real host: {bad}"

    @pytest.mark.parametrize("name", ["ack_poll.har", "text_stream.har"])
    def test_it_is_still_a_valid_har(self, name):
        d = json.loads((FIXTURES / name).read_text())
        assert d["log"]["entries"], "a HAR with no entries proves nothing"

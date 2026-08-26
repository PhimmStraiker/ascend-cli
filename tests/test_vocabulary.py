"""
One word per concept, everywhere the user can see.

Calling one thing by several names is a real defect, not a style preference: a reader who meets
"thin key", "tc key" and "bridge key" has to work out whether those are three things before they
can act. This suite pins the vocabulary in docs/GLOSSARY.md so it cannot drift back.

Scope is deliberately USER-FACING text — help output, printed messages, docs. Internal identifiers
(`cmd_relay_start`, the `relays` JSON back-compat key) are exempt: renaming those breaks callers
without helping any reader.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
CLI = REPO / "shells" / "cli" / "ascend.py"

# left = a synonym that must not appear; right = the one word for that thing
BANNED = {
    "thin key": "bridge key",
    "thin api key": "bridge key",
    "tc key": "bridge key",
    "relay key": "bridge key",
    "evidence log": "transcript",
    "recorded session": "transcript",
    "capture file": "transcript",
}

CANONICAL = ["bridge key", "transcript", "assessment", "adapter config", "control", "probe"]


def _all_help() -> str:
    """Every help screen the CLI can print, concatenated."""
    env = dict(os.environ, NO_COLOR="1", ASCEND_NO_SPINNER="1", STRAIKER_PAT="s6r_pat_test")
    groups = ("app controls assess adapter map chat onboard results export ci bridge keys "
              "tenant policy status doctor version").split()
    out = [subprocess.run([sys.executable, str(CLI), "--help"],
                          capture_output=True, text=True, env=env).stdout]
    for g in groups:
        r = subprocess.run([sys.executable, str(CLI), g, "--help"],
                           capture_output=True, text=True, env=env)
        out.append(r.stdout)
        for verb in re.findall(r"^\s{4}(\w[\w-]*)", r.stdout, re.M):
            out.append(subprocess.run([sys.executable, str(CLI), g, verb, "--help"],
                                      capture_output=True, text=True, env=env).stdout)
    return "\n".join(out)


@pytest.fixture(scope="module")
def help_text():
    return _all_help()


class TestHelpTextVocabulary:
    @pytest.mark.parametrize("banned,canonical", sorted(BANNED.items()))
    def test_no_synonym_survives_in_help(self, help_text, banned, canonical):
        assert banned not in help_text.lower(), \
            f"help text says {banned!r}; this tool calls that a {canonical!r}"

    def test_the_canonical_words_are_actually_used(self, help_text):
        low = help_text.lower()
        for term in CANONICAL:
            assert term in low, f"{term!r} is the canonical word but appears nowhere in help"

    def test_echoed_is_gone_from_user_facing_text(self, help_text):
        """"Echoed" told the reader nothing. FROM PROMPT / FROM TARGET is self-explanatory and
        sums to TIMES SEEN, so the arithmetic can be checked."""
        assert "echoed" not in help_text.lower()


class TestOutputVocabulary:
    def test_value_provenance_columns_are_the_documented_ones(self):
        src = (REPO / "shells" / "cli" / "ascend.py").read_text()
        assert "TIMES SEEN" in src and "FROM TARGET" in src and "FROM PROMPT" in src
        assert "ECHOED" not in src, "the column header must match the glossary"

    def test_markdown_and_terminal_use_the_same_words(self):
        """A report pasted from --md should read like what was on screen."""
        src = (REPO / "shells" / "cli" / "ascend.py").read_text()
        assert "| Control | Value | Times seen | From target | From prompt |" in src

    def test_passed_is_a_column_not_an_inference(self):
        """`probes - failed` silently counts unanswered probes as passes."""
        src = (REPO / "shells" / "cli" / "ascend.py").read_text()
        assert "'PASSED':>7" in src and "'UNANSW':>7" in src


class TestGlossaryIsRealAndComplete:
    def test_the_glossary_exists(self):
        assert (REPO / "docs" / "GLOSSARY.md").exists()

    @pytest.mark.parametrize("term", CANONICAL)
    def test_every_canonical_term_is_defined(self, term):
        doc = (REPO / "docs" / "GLOSSARY.md").read_text().lower()
        assert f"**{term}**" in doc or f"**{term}s**" in doc, \
            f"{term!r} is used by the CLI but not defined in the glossary"

    @pytest.mark.parametrize("banned", sorted(BANNED))
    def test_every_banned_synonym_is_listed_as_such(self, banned):
        """The glossary must say what NOT to call things, or the rule is unenforceable."""
        assert banned in (REPO / "docs" / "GLOSSARY.md").read_text().lower()

    def test_it_explains_controls_versus_gate_policy(self):
        """The pair that confused a real user."""
        doc = (REPO / "docs" / "GLOSSARY.md").read_text()
        assert "Controls vs the gate policy" in doc
        assert "belongs to the **platform**" in doc and "belongs to **you**" in doc


class TestOneResultsCommand:
    def test_results_takes_an_optional_file(self):
        """`results` (file) and `reports` (platform) as separate verbs was the worst collision:
        two commands, near-identical names, different jobs."""
        src = (REPO / "shells" / "cli" / "ascend.py").read_text()
        assert 's.add_argument("file", nargs="?"' in src

    def test_reports_still_works_as_an_alias(self, help_text):
        env = dict(os.environ, NO_COLOR="1", STRAIKER_PAT="s6r_pat_test")
        r = subprocess.run([sys.executable, str(CLI), "reports", "--help"],
                           capture_output=True, text=True, env=env)
        assert r.returncode == 0

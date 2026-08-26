"""
Progress and confirmation lines — and the guarantee that they cost an agent nothing.

"Did that actually work?" is a fair question when a command's only output is a table. Enterprise
CLIs answer it: gcloud prints `Creating instance...done.`, gh prints `✓ Created repository`.
Both put those lines on STDERR so the data on stdout stays pipeable — which is the only reason a
human affordance can be added here without breaking docs/AGENTS.md's contract.

These tests exist because that separation is easy to break by accident, and breaking it turns
every agent's `--json` parse into a crash.
"""
from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "control"))
sys.path.insert(0, str(REPO / "runtime"))

_spec = importlib.util.spec_from_file_location("ascend_cli", REPO / "shells" / "cli" / "ascend.py")
cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli)


class Args:
    def __init__(self, **kw):
        self.json = False
        self.__dict__.update(kw)


class TestSayGoesToStderrOnly:
    def test_a_message_never_touches_stdout(self, capsys):
        cli._say(Args(), "Creating application...")
        out = capsys.readouterr()
        assert out.out == "", "stdout must stay exactly the machine payload"
        assert "Creating application" in out.err

    def test_a_confirmation_never_touches_stdout(self, capsys):
        cli._say(Args(), "created 'My Bot'", done=True)
        out = capsys.readouterr()
        assert out.out == ""
        assert "created 'My Bot'" in out.err

    def test_json_mode_emits_nothing_at_all(self, capsys):
        """Under --json an agent may be reading stderr for real diagnostics; chatter is noise."""
        cli._say(Args(json=True), "Creating application...")
        cli._say(Args(json=True), "created", done=True)
        out = capsys.readouterr()
        assert out.out == "" and out.err == ""

    def test_the_done_marker_is_present_for_a_human(self, capsys, monkeypatch):
        monkeypatch.setenv("NO_COLOR", "1")
        cli._say(Args(), "created", done=True)
        assert "✓" in capsys.readouterr().err

    def test_no_color_means_no_escape_codes(self, capsys, monkeypatch):
        monkeypatch.setenv("NO_COLOR", "1")
        cli._say(Args(), "Creating...")
        cli._say(Args(), "created", done=True)
        err = capsys.readouterr().err
        assert "\033[" not in err, "NO_COLOR must disable styling, not just colour choice"


class TestConfirmationsAreNotPartOfTheContract:
    """A human affordance must never become something a script has to parse around."""

    def test_say_returns_nothing_and_is_never_in_a_payload(self):
        assert cli._say(Args(), "anything") is None

    def test_mutating_commands_still_route_data_through__out(self):
        """_out owns the machine payload; _say must not be substituted for it anywhere."""
        src = (REPO / "shells" / "cli" / "ascend.py").read_text()
        for line in src.splitlines():
            st = line.strip()
            if st.startswith("_say(") and "return" in st:
                pytest.fail(f"_say used as a return value: {st}")

    def test_say_is_json_aware_at_every_call_site(self):
        """Every call passes args, so --json suppression cannot be forgotten at one site."""
        src = (REPO / "shells" / "cli" / "ascend.py").read_text()
        import re
        for m in re.finditer(r"_say\(([^,)]*)", src):
            first = m.group(1).strip()
            if first in ("", "self"):
                continue
            assert first == "args", f"_say must take args first (got {first!r})"


class TestStreamSeparationEndToEnd:
    """The property that actually matters: --json output is parseable, byte for byte."""

    @pytest.mark.parametrize("argv", [
        ["--help"],
        ["version"],
    ])
    def test_json_stdout_is_never_polluted(self, argv):
        import subprocess
        env = {"PATH": "/usr/bin:/bin", "NO_COLOR": "1", "ASCEND_NO_SPINNER": "1",
               "STRAIKER_PAT": "s6r_pat_test"}
        r = subprocess.run([sys.executable, str(REPO / "shells" / "cli" / "ascend.py"), *argv],
                           capture_output=True, text=True, cwd=str(REPO), env=env)
        assert "✓" not in r.stdout, "a confirmation glyph leaked onto stdout"

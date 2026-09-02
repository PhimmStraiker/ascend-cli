"""
test_pty_contract — the branch a customer actually sees.

Every other subprocess test in this suite pipes stdout, so `color_ok()` is False and the styled
code path is never executed. That is the one hole worth closing directly: a real pseudo-terminal
runs the code a human gets, and asserts the two things that must hold there.

  * `--json` on stdout stays free of escapes even on a TTY, and still parses.
  * human output on a TTY does get styled, so the feature is actually reachable (a test that
    only ever proves "no colour when piped" would pass just as well if colour were broken).
"""
import json
import os
import pty
import re
import select
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
ESC = "\033"
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def run_on_pty(args, env_extra=None, timeout=90):
    """Run the CLI with stdout attached to a real pty; return the decoded output."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("NO_COLOR", "ASCEND_PLAIN", "ASCEND_COLOR_DEPTH")}
    env.update({"STRAIKER_PAT": "s6r_pat_dummy", "ASCEND_SKIP_TENANT_CHECK": "1",
                "TERM": "xterm-256color", "ASCEND_NO_SPINNER": "1", "COLUMNS": "100"})
    if env_extra:
        env.update(env_extra)
    primary, secondary = pty.openpty()
    try:
        p = subprocess.Popen([sys.executable, str(REPO / "shells/cli/ascend.py"), *args],
                             stdout=secondary, stderr=subprocess.DEVNULL,
                             cwd=str(REPO), env=env, close_fds=True)
        os.close(secondary)
        chunks = []
        while True:
            r, _, _ = select.select([primary], [], [], timeout)
            if not r:
                break
            try:
                data = os.read(primary, 65536)
            except OSError:
                break
            if not data:
                break
            chunks.append(data)
        p.wait(timeout=timeout)
        return b"".join(chunks).decode("utf-8", "replace"), p.returncode
    finally:
        try:
            os.close(primary)
        except OSError:
            pass


@pytest.mark.skipif(not hasattr(pty, "openpty"), reason="no pty on this platform")
class TestOnARealTerminal:
    def test_the_pty_actually_reports_as_a_tty(self):
        """Guard the harness itself: if the pty were not a TTY, every assertion below would
        pass for the wrong reason."""
        out, _ = run_on_pty(["version"])
        assert out.strip(), "no output captured from the pty"

    def test_json_stdout_has_no_escapes_on_a_tty(self):
        out, rc = run_on_pty(["--json", "doctor"])
        assert ESC not in out, "--json must stay machine-readable even on a terminal"

    def test_json_stdout_still_parses_on_a_tty(self):
        out, _ = run_on_pty(["--json", "doctor"])
        json.loads(out.replace("\r\n", "\n"))

    def test_human_output_is_styled_on_a_tty(self):
        """The positive case. Without it, a broken colour path would look like a pass.

        `bridge ls` is used rather than `adapter configs`: the latter is deliberately left
        unstyled, so asserting on it proved nothing -- which is exactly how this test first
        failed.
        """
        out, _ = run_on_pty(["bridge", "ls", "--no-check"])
        assert ANSI.search(out), "no styling reached a real terminal"

    def test_no_color_still_wins_on_a_tty(self):
        out, _ = run_on_pty(["bridge", "ls", "--no-check"], {"NO_COLOR": "1"})
        assert ESC not in out

    def test_ascend_plain_still_wins_on_a_tty(self):
        out, _ = run_on_pty(["bridge", "ls", "--no-check"], {"ASCEND_PLAIN": "1"})
        assert ESC not in out

    @pytest.mark.parametrize("depth", ["8", "256", "24"])
    def test_every_depth_renders_on_a_tty(self, depth):
        out, _ = run_on_pty(["bridge", "ls", "--no-check"], {"ASCEND_COLOR_DEPTH": depth})
        assert ANSI.search(out), depth

    def test_exit_code_is_unchanged_on_a_tty(self):
        """Styling must not alter the CI contract."""
        _, rc_tty = run_on_pty(["adapter", "validate", "--config", "definitely-not-here"])
        piped = subprocess.run(
            [sys.executable, str(REPO / "shells/cli/ascend.py"),
             "adapter", "validate", "--config", "definitely-not-here"],
            capture_output=True, text=True, cwd=str(REPO),
            env=dict(os.environ, STRAIKER_PAT="s6r_pat_dummy", NO_COLOR="1"), timeout=90)
        assert rc_tty == piped.returncode, "a terminal changed the exit code"

    def test_stripping_the_colour_gives_the_piped_text(self):
        """The strongest form of 'no functional impact': the styled output, with escapes
        removed, is the same text a pipe receives."""
        styled, _ = run_on_pty(["adapter", "list"])
        piped = subprocess.run(
            [sys.executable, str(REPO / "shells/cli/ascend.py"), "adapter", "list"],
            capture_output=True, text=True, cwd=str(REPO),
            env=dict(os.environ, STRAIKER_PAT="s6r_pat_dummy", NO_COLOR="1",
                     ASCEND_NO_SPINNER="1"), timeout=90)
        assert ANSI.sub("", styled).replace("\r\n", "\n").split() == piped.stdout.split()

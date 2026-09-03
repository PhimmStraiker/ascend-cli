"""
test_launcher_and_adopt.py — two fixes reported by @ryan-straiker in PR #36.

Both are about a first run going wrong in a way the tool never explains.

1. THE LAUNCHER IGNORED THE CLONE'S OWN VENV.

   `./ascend` resolved `python3` off PATH before looking at `.venv/bin/python` sitting right
   beside it. The CLI's one hard dependency is `requests`, which a system python3 almost never
   has, so a fresh clone died on

       ModuleNotFoundError: No module named 'requests'

   with a working virtualenv one directory away. That is the first command a new user runs, and
   the error names a Python package rather than the thing that is actually wrong.

   The order that matters is: explicit `$ASCEND_PYTHON` > the clone's `.venv` > PATH. The
   override has to keep winning, or anyone pinning an interpreter loses it the moment a `.venv`
   appears.

2. `target add --app <existing>` RESOLVED A CONTROL CATALOG IT THEN THREW AWAY.

   The control set was resolved BEFORE the branch that decides whether to create an app or adopt
   one that already exists. The adopt path ignores `--controls` entirely — it says so in its own
   output, two lines further down — so adopting cost a wasted `list_controls()` round trip and,
   worse, printed

       no --controls given — registering with all 62 catalog controls

   while registering nothing at all. A log line that describes an action the command did not take
   is how someone ends up debugging the wrong half of a problem.

   This one is easy to reintroduce: the fix is a line's POSITION, not its content, so it survives
   any test that only checks the resolution happens. Hence the source-discipline test below.
"""
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "shells" / "cli"))
LAUNCHER = REPO / "ascend"
SRC = (REPO / "shells" / "cli" / "ascend.py").read_text()


def _clone(tmp_path, *, venv=True):
    """A miniature clone: the real launcher, a stub entry point, optionally a stub venv."""
    (tmp_path / "shells" / "cli").mkdir(parents=True)
    (tmp_path / "shells" / "cli" / "ascend.py").write_text("")
    sh = tmp_path / "ascend"
    sh.write_text(LAUNCHER.read_text())
    sh.chmod(0o755)
    if venv:
        b = tmp_path / ".venv" / "bin"
        b.mkdir(parents=True)
        p = b / "python"
        p.write_text("#!/bin/bash\necho VENV\n")
        p.chmod(0o755)
    return sh


def _run(sh, env=None):
    import os
    e = {**os.environ, **(env or {})}
    e.pop("ASCEND_PYTHON", None) if not (env or {}).get("ASCEND_PYTHON") else None
    return subprocess.run([str(sh), "doctor"], capture_output=True, text=True, env=e).stdout


class TestTheLauncherPrefersTheClonesVenv:
    def test_a_clone_with_a_venv_uses_it(self, tmp_path):
        assert "VENV" in _run(_clone(tmp_path)), (
            "the launcher fell through to PATH python3 with a virtualenv sitting beside it — "
            "a fresh clone will die on 'No module named requests'")

    def test_no_venv_still_falls_through_to_path(self, tmp_path):
        """The fix must not make a venv mandatory; pipx and system installs have none."""
        out = _run(_clone(tmp_path, venv=False))
        assert "VENV" not in out

    def test_an_explicit_interpreter_still_wins(self, tmp_path):
        other = tmp_path / "other"
        other.write_text("#!/bin/bash\necho OVERRIDE\n")
        other.chmod(0o755)
        out = _run(_clone(tmp_path), env={"ASCEND_PYTHON": str(other)})
        assert "OVERRIDE" in out and "VENV" not in out, (
            "ASCEND_PYTHON must beat the venv, or anyone pinning an interpreter silently loses it")

    def test_a_non_executable_venv_python_is_skipped(self, tmp_path):
        sh = _clone(tmp_path)
        (tmp_path / ".venv" / "bin" / "python").chmod(0o644)
        assert "VENV" not in _run(sh), "an unusable venv python must not be selected"

    def test_the_launcher_checks_executability_not_just_existence(self):
        assert "-x " in LAUNCHER.read_text(), (
            "testing with -f would select a non-executable file and fail with a worse error")


class TestAdoptingAnAppDoesNotResolveTheCatalog:
    def _onboard(self):
        m = re.search(r"^def cmd_onboard\(args\):(.*?)(?=^def )", SRC, re.S | re.M)
        assert m, "cmd_onboard not found"
        return m.group(1)

    def test_the_resolution_happens_after_the_adopt_branch(self):
        body = self._onboard()
        adopt = body.index("existing_ref = getattr(args")
        resolve = body.index("_resolve_all_controls(")
        assert resolve > adopt, (
            "the control catalog is resolved before the create/adopt branch, so "
            "`target add --app <existing>` makes a wasted list_controls() call and prints "
            "'registering with all N catalog controls' while registering nothing")

    def test_it_is_resolved_on_the_create_path(self):
        """Moving it must not lose it: creating still needs an explicit id list or the API 400s."""
        body = self._onboard()
        after = body[body.index("_resolve_all_controls("):]
        assert "build_thin_spec" in after, "the resolved controls no longer reach the create call"

    def test_the_adopt_path_still_says_controls_are_ignored(self):
        assert "ignored when adopting an existing app" in self._onboard()

    def test_it_is_resolved_exactly_once(self):
        assert self._onboard().count("_resolve_all_controls(") == 1

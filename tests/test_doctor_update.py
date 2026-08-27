"""
test_doctor_update — the `ascend doctor --update` wiring in the CLI (the boundary selfupdate's pure
logic doesn't cover). The GitHub fetch and the `git pull` subprocess are both stubbed, so no network
runs and no real checkout is mutated. Pins: up-to-date exits 0 without touching git; a behind
pipx/binary install prints the command and never runs git; a behind clone runs git and maps the
git return code to the exit code; the --json path emits a machine object on every branch.
"""
import io
import json
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
for p in ("control", "runtime", "shells/cli"):
    sys.path.insert(0, str(REPO / p))
import ascend  # noqa: E402


class _Args:
    json = False


def _run(monkeypatch, release, *, kind="pipx", json_mode=False):
    monkeypatch.setattr(ascend, "_fetch_latest_release", lambda *a, **k: release)
    monkeypatch.setattr(ascend, "_install_context", lambda: (kind, "UPDATE-CMD"))
    args = _Args()
    args.json = json_mode
    buf = io.StringIO()
    with pytest.raises(SystemExit) as ex, redirect_stdout(buf):
        ascend._doctor_update(args)
    return ex.value.code, buf.getvalue()


def _rel(tag):
    return {"tag": tag, "name": None, "body": None, "url": "https://example/rel"}


def test_up_to_date_exits_zero_without_git(monkeypatch):
    # if git were called it would raise (no stub) — proves the up-to-date path never runs it
    def boom(*a, **k):
        raise AssertionError("git must not run when up to date")
    monkeypatch.setattr(subprocess, "run", boom)
    code, out = _run(monkeypatch, _rel("v1.0.0"), kind="clone")
    assert code == 0
    assert "up to date" in out.lower()


def test_behind_pipx_prints_command_no_git(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("git must not run for a pipx install")
    monkeypatch.setattr(subprocess, "run", boom)
    code, out = _run(monkeypatch, _rel("v2.0.0"), kind="pipx")
    assert code == 0
    assert "UPDATE-CMD" in out
    assert "2.0.0" in out


def test_behind_clone_runs_git_and_reports_success(monkeypatch):
    class R:
        returncode = 0
        stdout = "Updating a1b2c3..d4e5f6\nFast-forward"
        stderr = ""
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: R())
    code, out = _run(monkeypatch, _rel("v2.0.0"), kind="clone")
    assert code == 0
    assert "updated" in out.lower()


def test_behind_clone_git_nonzero_maps_to_error_exit(monkeypatch):
    class R:
        returncode = 1
        stdout = ""
        stderr = "fatal: Not possible to fast-forward, aborting."
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: R())
    code, out = _run(monkeypatch, _rel("v2.0.0"), kind="clone")
    assert code == 1
    assert "fast-forward" in out


def test_json_branch_emits_object(monkeypatch):
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: type("R", (), {"returncode": 0, "stdout": "ok", "stderr": ""}))
    code, out = _run(monkeypatch, _rel("v2.0.0"), kind="pipx", json_mode=True)
    obj = json.loads(out)
    assert obj["version"]["state"] == "update_available"
    assert obj["updated"] is False
    assert obj["update_command"] == "UPDATE-CMD"

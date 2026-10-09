"""
test_config_rerun_flags.py — the auth flags apply on a `--config` re-run, not only on a derive.

MEASURED 2026-10-09: `target add --config lab-session --header 'x-lab-code: <code>'` registered
an application whose record carried Content-Type, Cookie and X-Conv-Token — everything the saved
config had, and nothing the command line added. Every other source branch folds `--header` /
`--bearer` / `--api-key` / `--basic` / `--cookie` into the config it derives; the `--config`
branch loaded the file and dropped the flags on the floor, silently. The fix is the same three
steps the other branches run, applied to the loaded config and written back only when something
changed, so a plain `--config` re-run stays byte-identical.
"""
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
for _p in (REPO / "shells" / "cli", REPO / "runtime", REPO / "control", REPO):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
import ascend  # noqa: E402
from _onboard_harness import FakePlatform, run_target_add  # noqa: E402


@pytest.fixture(autouse=True)
def _public_target(monkeypatch):
    """The transport choice asks DNS whether the target is public; the harness poisons DNS."""
    monkeypatch.setattr(ascend, "_is_public_host", lambda url: True)

CFG = {"adapter": "direct_api", "endpoint": "https://lab.example.com/session/api/messages",
       "method": "POST", "body": {"message": "{{PROMPT}}"}, "response_path": "reply",
       "headers": {"Content-Type": "application/json"}}


def _written(tmp_path):
    return json.loads((tmp_path / "mybot.json").read_text())


def _hdrs(record):
    """A record's headers as a dict: the create spec carries them as [{name, value}] on the wire."""
    h = record.get("headers") or {}
    return {x["name"]: x["value"] for x in h} if isinstance(h, list) else dict(h)


class TestAHeaderFlagOnAConfigRerun:
    def test_it_lands_on_the_record_and_in_the_saved_config(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setenv("ASCEND_CONFIG_DIR", str(tmp_path))
        platform = FakePlatform()
        run_target_add(monkeypatch, tmp_path, CFG, platform, name="lab-session",
                       header=["x-lab-code: lab-code-9f13c2"])
        (spec,) = platform.created
        assert _hdrs(spec)["x-lab-code"] == "lab-code-9f13c2"
        assert _hdrs(spec)["Content-Type"] == "application/json"
        assert _written(tmp_path)["headers"]["x-lab-code"] == "lab-code-9f13c2"
        assert "applied x-lab-code from the command line" in capsys.readouterr().err

    def test_a_plain_rerun_leaves_the_file_byte_identical(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ASCEND_CONFIG_DIR", str(tmp_path))
        run_target_add(monkeypatch, tmp_path, CFG, FakePlatform(), name="lab-session")
        before = (tmp_path / "mybot.json").read_bytes()
        run_target_add(monkeypatch, tmp_path, CFG, FakePlatform(), name="lab-session")
        assert (tmp_path / "mybot.json").read_bytes() == before

    def test_an_env_reference_joins_the_captured_credential_instead_of_replacing_it(self, monkeypatch, tmp_path):
        """`--header 'x-lab-code: env:LAB_CODE'` beside a captured Cookie is the ordinary case."""
        monkeypatch.setenv("ASCEND_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("LAB_CODE", "lab-code-9f13c2")
        monkeypatch.setenv("ASCEND_SECRET_LAB_EXAMPLE_COM_COOKIE", "lab_access=1")
        cfg = {**CFG, "auth": {"type": "static", "mode": "headers",
                               "headers": {"Cookie": "env:ASCEND_SECRET_LAB_EXAMPLE_COM_COOKIE"}}}
        platform = FakePlatform()
        run_target_add(monkeypatch, tmp_path, cfg, platform, name="lab-session",
                       header=["x-lab-code: env:LAB_CODE"])
        saved = _written(tmp_path)
        assert isinstance(saved["auth"], list) and len(saved["auth"]) == 2, saved["auth"]
        assert "lab-code-9f13c2" not in json.dumps(saved), "a reference, never the value"
        (spec,) = platform.created
        assert _hdrs(spec)["x-lab-code"] == "lab-code-9f13c2"
        assert _hdrs(spec)["Cookie"] == "lab_access=1"

    def test_an_api_key_flag_is_applied_the_same_way(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ASCEND_CONFIG_DIR", str(tmp_path))
        platform = FakePlatform()
        run_target_add(monkeypatch, tmp_path, CFG, platform, name="lab-session",
                       api_key="X-Api-Key:k-0123456789")
        (spec,) = platform.created
        assert _hdrs(spec)["X-Api-Key"] == "k-0123456789"

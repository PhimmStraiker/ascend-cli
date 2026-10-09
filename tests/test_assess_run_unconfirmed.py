"""
`assess run` after a connection error: keep watching, and diagnose a stall against what was measured.

Two things this pins:

  * A transport error during the settle window used to come out of `confirm_started` as
    `started: False` — the same answer as "the platform put the run back to paused" — so `run()`
    RETURNED and `assess run` reported a run that was very likely fine as one that never started.
    Now nothing-could-be-read is `started: None` with `unconfirmed: True`, a caller that asked to
    wait keeps polling (the poll already tolerates transport errors), and only a read settles it.

  * The stall diagnosis is derived from the platform's measured model, not the one the comments
    used to state: a run born paused goes `running -> paused` 15-85 s after creation, but a direct
    target that fails every probe is paused only after 5 consecutive failures AND a ~180 s
    cooldown — longer than the 45 s settle window, so `started: True` says nothing about the
    target. An adaptor app cannot be replayed from here at all; its diagnosis points at the
    engine's own verifier.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
for p in ("shells/cli", "runtime", "control"):
    if str(REPO / p) not in sys.path:
        sys.path.insert(0, str(REPO / p))
import api  # noqa: E402
import ascend  # noqa: E402

API_SRC = (REPO / "control" / "api.py").read_text()


class Dropped(Exception):
    """requests' ConnectionError, as far as the client is concerned."""


def _client(script):
    c = api.AscendAPI.__new__(api.AscendAPI)
    seq = list(script)

    def get_assessment(app_id, aid):
        item = seq.pop(0) if seq else script[-1]
        if isinstance(item, Exception):
            raise item
        return item
    c.get_assessment = get_assessment
    c.pause = lambda app_id, aid: None
    c.resume = lambda app_id, aid: None
    c.create_assessment = lambda app_id, name: {"id": "asmt_1"}
    c.live_assessment = lambda app_id: None
    return c


class TestTheSettleWindow:
    def test_nothing_read_is_unknown_not_a_failed_start(self, monkeypatch):
        monkeypatch.setattr(api.time, "sleep", lambda s: None)
        c = _client([Dropped("Remote end closed connection")])
        out = c.confirm_started("aapp", "asmt_1", settle=0, every=0)
        assert out["started"] is None and out["unconfirmed"] is True
        assert out["status"] == "unknown" and "Dropped" in out["error"]

    def test_one_blip_then_a_read_is_a_normal_answer(self, monkeypatch):
        monkeypatch.setattr(api.time, "sleep", lambda s: None)
        ticks = iter([0, 1, 2, 3, 99, 99, 99])
        monkeypatch.setattr(api.time, "time", lambda: next(ticks))
        c = _client([Dropped("blip"), {"status": "running"}, {"status": "running"}])
        out = c.confirm_started("aapp", "asmt_1", settle=10, every=1)
        assert out == {"status": "running", "started": True, "auto_paused": False}

    def test_a_platform_pause_is_still_a_failed_start(self, monkeypatch):
        monkeypatch.setattr(api.time, "sleep", lambda s: None)
        ticks = iter([0, 1, 2, 3, 99, 99, 99])
        monkeypatch.setattr(api.time, "time", lambda: next(ticks))
        c = _client([{"status": "running"}, {"status": "paused"}, {"status": "paused"}])
        out = c.confirm_started("aapp", "asmt_1", settle=10, every=1)
        assert out["started"] is False and out["auto_paused"] is True


class TestRunKeepsWatching:
    def test_a_wait_keeps_polling_when_the_start_could_not_be_read(self, monkeypatch):
        """The live failure: connection error after the create -> the run must be followed, not
        reported as not started."""
        monkeypatch.setattr(api.time, "sleep", lambda s: None)
        c = _client([Dropped("after create"), {"status": "running"}, {"status": "complete", "total": 4}])
        out = c.run("aapp", "r", wait=True, settle=0, interval=1)
        assert out["status"] == "complete" and out["started"] is True
        assert "unconfirmed" not in out

    def test_no_wait_returns_the_unknown_honestly(self, monkeypatch):
        monkeypatch.setattr(api.time, "sleep", lambda s: None)
        c = _client([Dropped("after create")])
        out = c.run("aapp", "r", wait=False, settle=0)
        assert out["started"] is None and out["unconfirmed"] is True
        assert out["assessment_id"] == "asmt_1"

    def test_a_run_the_platform_paused_still_returns_at_once(self, monkeypatch):
        monkeypatch.setattr(api.time, "sleep", lambda s: None)
        c = _client([{"status": "paused"}] * 5)
        out = c.run("aapp", "r", wait=True, settle=0, interval=1)
        assert out["started"] is False and out["status"] == "paused"

    def test_the_comments_state_the_measured_model(self):
        """The old claim ("paused within 10-20 seconds") was the whole basis of a 45 s window."""
        assert "10-20 seconds" not in API_SRC
        assert "180 s" in API_SRC and "5 consecutive" in API_SRC

    def test_the_command_says_when_the_start_was_not_read(self):
        src = ascend.SRC if hasattr(ascend, "SRC") else (REPO / "shells" / "cli" / "ascend.py").read_text()
        body = src[src.index("def cmd_assess_run("):src.index("def _diagnose_not_started(")]
        assert 'res.get("unconfirmed")' in body and "could not be read" in body


class TestTheStallDiagnosis:
    class _Platform:
        def __init__(self, app):
            self.app = app

        def get_app(self, app_id):
            return self.app

    def test_an_adaptor_app_is_sent_to_the_engines_verifier_not_replayed(self, monkeypatch):
        import requests
        monkeypatch.setattr(requests, "post", lambda *a, **k: (_ for _ in ()).throw(AssertionError("replayed")))
        app = {"id": "aapp_1", "name": "Lab Bot", "api_type": "api", "url": "https://bot.example.com/chat",
               "request_template": '{"message": "{{PROMPT}}", "_adaptor_src": "c3Jj"}'}
        why = ascend._diagnose_not_started(self._Platform(app), "aapp_1", {"stalled": True})
        assert "ascend adaptor verify --app 'Lab Bot'" in why
        assert "180 s" in why and "5 consecutive" in why
        assert "cannot be replayed from here" in why

    def test_a_run_that_never_started_is_not_blamed_on_the_tolerance(self):
        app = {"id": "aapp_1", "name": "Lab Bot", "api_type": "api", "url": "https://bot.example.com/chat",
               "request_template": '{"message": "{{PROMPT}}", "_adaptor_src": "c3Jj"}'}
        why = ascend._diagnose_not_started(self._Platform(app), "aapp_1", {"started": False})
        assert "180 s" not in why and "adaptor verify" in why

    def test_a_stalled_template_app_says_what_the_platform_retries_and_what_it_does_not(self, monkeypatch):
        import requests

        class R:
            status_code = 401
            text = "bad code"
        monkeypatch.setattr(requests, "post", lambda *a, **k: R())
        app = {"id": "aapp_1", "name": "Lab Bot", "api_type": "api", "url": "https://bot.example.com/chat",
               "request_template": '{"message": "{{PROMPT}}"}', "headers": [{"name": "x-k", "value": "v"}]}
        why = ascend._diagnose_not_started(self._Platform(app), "aapp_1", {"stalled": True})
        assert "HTTP 401" in why and "after its tolerance" in why
        assert "5xx or a timeout is retried" in why and "a 4xx is not" in why
        assert "ascend assess resume" in why

    def test_a_bridge_app_still_points_at_the_bridge(self):
        app = {"id": "aapp_1", "api_type": "thin"}
        why = ascend._diagnose_not_started(self._Platform(app), "aapp_1", {"stalled": True})
        assert "bridge ls" in why and "180 s" in why

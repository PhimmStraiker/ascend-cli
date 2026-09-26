"""
When a run does not start, the CLI says why — by testing, not guessing.

The platform records no reason when it puts a run back to paused, so `_diagnose_not_started`
replays the application's own request once and reports what came back. The first time this
function was ever reached it crashed with a NameError, because nothing had run it. These tests
run every branch.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
for part in ("shells/cli", "runtime", "control"):
    sys.path.insert(0, str(REPO / part))
sys.path.insert(0, str(REPO))
import ascend  # noqa: E402

BODY = '{"message": "{{PROMPT}}"}'
DIRECT = {"api_type": "api", "url": "https://bot.example.com/chat", "request_template": BODY,
          "headers": [{"name": "x-demo-key", "value": "pass"}]}


class Client:
    def __init__(self, app):
        self.app = app

    def get_app(self, app_id):
        if isinstance(self.app, Exception):
            raise self.app
        return self.app


class Reply:
    def __init__(self, status, text=""):
        self.status_code, self.text = status, text


@pytest.fixture
def sent(monkeypatch):
    """Capture the replayed request instead of sending it."""
    import requests
    calls = []

    def fake_post(url, data=None, headers=None, timeout=None):
        calls.append({"url": url, "data": data, "headers": headers})
        return fake_post.reply
    fake_post.reply = Reply(200, "ok")
    monkeypatch.setattr(requests, "post", fake_post)
    return calls, fake_post


def test_a_bridge_app_points_at_the_relay():
    out = ascend._diagnose_not_started(Client({"api_type": "thin"}), "aapp_1", {})
    assert "bridge" in out and "ascend bridge ls" in out


def test_an_app_without_an_address_says_so():
    out = ascend._diagnose_not_started(Client({"api_type": "api"}), "aapp_1", {})
    assert "no url" in out


def test_a_refusal_from_the_target_is_reported_with_its_status(sent):
    calls, post = sent
    post.reply = Reply(401, '{"detail": "key required"}')
    out = ascend._diagnose_not_started(Client(DIRECT), "aapp_1", {})
    assert "HTTP 401" in out and "key required" in out
    assert "ascend target add" in out, "it must say how to fix it"


def test_the_replay_uses_the_apps_own_contract(sent):
    calls, _ = sent
    ascend._diagnose_not_started(Client(DIRECT), "aapp_1", {})
    assert calls[0]["url"] == "https://bot.example.com/chat"
    assert calls[0]["headers"] == {"x-demo-key": "pass"}
    assert b"{{PROMPT}}" not in calls[0]["data"], "the placeholder must be filled before sending"


def test_a_target_that_accepts_the_contract_points_at_the_platform_side(sent):
    out = ascend._diagnose_not_started(Client(DIRECT), "aapp_1", {})
    assert "HTTP 200" in out and "platform" in out


def test_a_masked_header_is_not_replayed(sent):
    calls, _ = sent
    app = {**DIRECT, "headers": [{"name": "Authorization", "value": "********"}]}
    out = ascend._diagnose_not_started(Client(app), "aapp_1", {})
    assert "masked" in out and "Authorization" in out
    assert calls == [], "a masked value would be sent as a wrong credential"


def test_a_network_failure_is_a_sentence_not_a_crash(monkeypatch):
    import requests

    def boom(*a, **kw):
        raise requests.ConnectionError("no route")
    monkeypatch.setattr(requests, "post", boom)
    out = ascend._diagnose_not_started(Client(DIRECT), "aapp_1", {})
    assert "could not replay" in out


def test_an_unreadable_app_is_not_a_crash():
    out = ascend._diagnose_not_started(Client(RuntimeError("down")), "aapp_1", {})
    assert isinstance(out, str) and out

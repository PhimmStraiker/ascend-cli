"""test_auth_refresh — the relay re-mints a short-lived oauth2 token mid-run (B5) so a long
assessment never sends an expired token; static/none auth is untouched."""
import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "control"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))
import call_target


def _caller(auth, refresh_ms=None):
    cfg = {"adapter": "direct_api", "endpoint": "http://x/chat",
           "body": {"message": "{{PROMPT}}"}, "response_path": "r"}
    if auth:
        cfg["auth"] = auth
    if refresh_ms is not None:
        cfg["auth_refresh_ms"] = refresh_ms
    return call_target.TargetCaller("direct_api", "inline", config=cfg)


def test_static_auth_never_refreshes(monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr(call_target, "merge_auth", lambda c: (calls.__setitem__("n", calls["n"] + 1) or c))
    tc = _caller({"type": "static", "mode": "bearer", "value": "env:X"})
    before = calls["n"]
    tc._maybe_refresh_auth(); tc._maybe_refresh_auth()
    assert calls["n"] == before          # static: no re-materialize


def test_oauth2_refreshes_after_ttl(monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr(call_target, "merge_auth", lambda c: (calls.__setitem__("n", calls["n"] + 1) or c))
    tc = _caller({"type": "oauth2", "grant": "client_credentials", "token_url": "http://t"},
                 refresh_ms=0)   # refresh due immediately
    n0 = calls["n"]
    tc._maybe_refresh_auth()
    assert calls["n"] == n0 + 1          # oauth2 + TTL elapsed -> re-materialized


def test_oauth2_does_not_refresh_before_ttl(monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr(call_target, "merge_auth", lambda c: (calls.__setitem__("n", calls["n"] + 1) or c))
    tc = _caller({"type": "oauth2", "grant": "client_credentials", "token_url": "http://t"},
                 refresh_ms=3_600_000)   # 1h -> not due
    n0 = calls["n"]
    tc._maybe_refresh_auth()
    assert calls["n"] == n0              # too soon -> no refresh

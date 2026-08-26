"""P0.3 regression: `adapter validate` authenticated but `runtime start`/`chat` did not.

A config with an `auth` block validated `ok=True`, then every relayed probe went out
UNAUTHENTICATED (100% 401, or a wrong-tier 200). These tests assert validate and the live
relay path (TargetCaller) resolve auth identically and actually attach the credential.
"""
import sys, os, asyncio, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "runtime"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pytest


AUTH_CFG = {
    "adapter": "direct_api",
    "endpoint": "https://api.example.com/chat",
    "method": "POST",
    "body": {"message": "{{PROMPT}}"},
    "response_path": "reply",
    "auth": {"type": "static", "mode": "bearer", "value_ref": "env:PARITY_TOKEN"},
}


def test_merge_auth_attaches_bearer_from_env(monkeypatch):
    monkeypatch.setenv("PARITY_TOKEN", "sekret-123")
    import dispatch
    merged = dispatch.merge_auth(AUTH_CFG)
    hdrs = {k.lower(): v for k, v in (merged.get("headers") or {}).items()}
    assert hdrs.get("authorization") == "Bearer sekret-123"


def test_relay_and_validate_send_identical_headers(monkeypatch):
    """The heart of it: capture the header the adapter actually puts on the wire in the
    RELAY path, and confirm it carries the resolved bearer."""
    monkeypatch.setenv("PARITY_TOKEN", "sekret-123")
    import call_target, adapters.direct_api as da

    seen = {}
    class FakeResp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"reply": "hi"}
    def fake_request(method, url, headers=None, timeout=None, **kw):
        seen["headers"] = headers or {}
        return FakeResp()
    monkeypatch.setattr(da.requests, "request", fake_request)

    tc = call_target.TargetCaller("direct_api", "inline", config=dict(AUTH_CFG))
    status, body = tc.handler({"payload": {"body": {"prompt": "hello"}, "headers": {}}})
    tc.reset()
    assert status == 200
    sent = {k.lower(): v for k, v in seen["headers"].items()}
    assert sent.get("authorization") == "Bearer sekret-123", \
        "RELAY sent the probe UNAUTHENTICATED — the P0.3 seam regressed"


def test_no_auth_block_is_a_noop():
    import dispatch
    cfg = {"adapter": "direct_api", "endpoint": "https://x/y"}
    assert dispatch.merge_auth(cfg) is cfg


def test_auth_type_none_is_a_noop():
    import dispatch
    cfg = {"adapter": "direct_api", "auth": {"type": "none"}}
    assert dispatch.merge_auth(cfg) is cfg


def test_auth_failure_is_annotated_not_raised(monkeypatch):
    """A missing env secret must not crash the relay — it's surfaced as _auth_error."""
    monkeypatch.delenv("PARITY_TOKEN", raising=False)
    import dispatch
    merged = dispatch.merge_auth(AUTH_CFG)
    assert "_auth_error" in merged

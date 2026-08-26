"""Regression tests for the generic session_poll adapter (create->send->GET-poll)."""
import sys, os, asyncio
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "runtime"))
import pytest
from adapters.session_poll import SessionPollAdapter, _render


def run_async(coro): return asyncio.get_event_loop().run_until_complete(coro)


class _Resp:
    def __init__(self, js, status=200): self._js=js; self.status_code=status
    def json(self): return self._js
    def raise_for_status(self):
        if self.status_code>=400: import requests; raise requests.HTTPError(response=self)


def test_render_substitutes_prompt_and_conv():
    prompt = chr(34) + "hi" + chr(34) + " with quotes"
    out = _render({"m": "{{PROMPT}}", "c": "{{CONV}}"}, prompt, "conv-1")
    assert out["m"] == prompt and out["c"] == "conv-1"


def _install(monkeypatch, script):
    """script: dict of (method, url_substr) -> response js, with a mutable poll sequence."""
    import adapters.session_poll as sp
    calls={"n":0}
    def fake_request(method, url, **kw):
        method=method.upper()
        if "/create" in url: return _Resp({"conversation_id":"conv-xyz"})
        if "/send" in url: return _Resp({"accepted":True})
        if "/messages" in url:
            calls["n"]+=1
            # first two polls: only the user turn; third: bot reply appears
            if calls["n"]>=3:
                return _Resp({"messages":[{"role":"user","text":"hi"},
                                          {"role":"assistant","text":"the answer"}]})
            return _Resp({"messages":[{"role":"user","text":"hi"}]})
        return _Resp({}, 404)
    monkeypatch.setattr(sp.requests, "request", fake_request)


def test_session_poll_waits_for_async_reply(monkeypatch):
    _install(monkeypatch, {})
    cfg={"create":{"url":"http://t/create","extract":"conversation_id"},
         "send":{"url":"http://t/{{CONV}}/send","body":{"message":"{{PROMPT}}"}},
         "poll":{"url":"http://t/{{CONV}}/messages","method":"GET","list_path":"messages",
                 "role_field":"role","bot_roles":["assistant"],"text_path":"text",
                 "interval_ms":1,"timeout_ms":5000}}
    r=run_async(SessionPollAdapter().send_prompt("hello", cfg))
    assert r["success"] is True
    assert r["response"]=="the answer"
    assert r["metadata"].get("conv")=="conv-xyz"


def test_session_poll_requires_all_three_urls(monkeypatch):
    _install(monkeypatch, {})
    r=run_async(SessionPollAdapter().send_prompt("x", {"create":{"url":"http://t/create"}}))
    assert r["success"] is False and "session_poll needs" in r["error"]


def test_session_poll_timeout_when_no_reply(monkeypatch):
    import adapters.session_poll as sp
    def fake(method,url,**kw):
        if "/create" in url: return _Resp({"conversation_id":"c1"})
        if "/send" in url: return _Resp({"ok":True})
        return _Resp({"messages":[{"role":"user","text":"hi"}]})  # bot never replies
    monkeypatch.setattr(sp.requests,"request",fake)
    cfg={"create":{"url":"http://t/create","extract":"conversation_id"},
         "send":{"url":"http://t/{{CONV}}/send"},
         "poll":{"url":"http://t/{{CONV}}/messages","interval_ms":1,"timeout_ms":50}}
    r=run_async(SessionPollAdapter().send_prompt("x", cfg))
    assert r["success"] is False and "no agent reply" in r["error"]


# --- knobs added after the live bot hunt (form encoding, ordered bootstrap, POST-poll) ---

def test_form_encoded_steps_use_data_not_json(monkeypatch):
    """Several major vendors speak form-urlencoded; sending JSON silently fails."""
    import adapters.session_poll as sp
    seen, sent = [], {"done": False}
    def fake(method, url, **kw):
        seen.append((url, "data" in kw, "json" in kw))
        if "/create" in url: return _Resp({"conversation_id": "c1"})
        if "/send" in url:
            sent["done"] = True
            return _Resp({"ok": True})
        # the reply only exists after the send — this is the watermark the adapter uses
        msgs = [{"role": "user", "text": "hello"}]
        if sent["done"]:
            msgs.append({"role": "assistant", "text": "hi"})
        return _Resp({"messages": msgs})
    monkeypatch.setattr(sp.requests, "request", fake)
    form = {"Content-Type": "application/x-www-form-urlencoded"}
    cfg = {"create": {"url": "http://t/create", "headers": form, "extract": "conversation_id"},
           "send": {"url": "http://t/{{CONV}}/send", "headers": form, "body": {"m": "{{PROMPT}}"}},
           "poll": {"url": "http://t/{{CONV}}/messages", "interval_ms": 1, "timeout_ms": 500}}
    r = run_async(SessionPollAdapter().send_prompt("hello", cfg))
    assert r["success"] is True
    create_call = [c for c in seen if "/create" in c[0]][0]
    send_call = [c for c in seen if "/send" in c[0]][0]
    assert create_call[1] is True and create_call[2] is False, "create should be form-encoded"
    assert send_call[1] is True and send_call[2] is False, "send should be form-encoded"


def test_ordered_bootstrap_runs_before_create(monkeypatch):
    """Vendors that gate on a fixed call sequence (ping -> connect -> open)."""
    import adapters.session_poll as sp
    order, sent = [], {"done": False}
    def fake(method, url, **kw):
        order.append(url)
        if "/create" in url: return _Resp({"conversation_id": "c1"})
        if "/send" in url:
            sent["done"] = True
            return _Resp({"ok": True})
        if "/messages" in url:
            msgs = [{"role": "user", "text": "x"}]
            if sent["done"]:
                msgs.append({"role": "assistant", "text": "done"})
            return _Resp({"messages": msgs})
        return _Resp({"ok": True})
    monkeypatch.setattr(sp.requests, "request", fake)
    cfg = {"bootstrap": [{"url": "http://t/ping"}, {"url": "http://t/connect"}],
           "create": {"url": "http://t/create", "extract": "conversation_id"},
           "send": {"url": "http://t/{{CONV}}/send"},
           "poll": {"url": "http://t/{{CONV}}/messages", "interval_ms": 1, "timeout_ms": 500}}
    r = run_async(SessionPollAdapter().send_prompt("x", cfg))
    assert r["success"] is True
    assert order[0].endswith("/ping") and order[1].endswith("/connect")
    assert order.index("http://t/create") > 1


def test_required_bootstrap_failure_is_fatal(monkeypatch):
    import adapters.session_poll as sp
    def fake(method, url, **kw):
        if "/ping" in url:
            import requests as R; raise R.RequestException("boom")
        return _Resp({"conversation_id": "c1"})
    monkeypatch.setattr(sp.requests, "request", fake)
    cfg = {"bootstrap": [{"url": "http://t/ping"}],
           "create": {"url": "http://t/create"}, "send": {"url": "http://t/s"},
           "poll": {"url": "http://t/m"}}
    r = run_async(SessionPollAdapter().send_prompt("x", cfg))
    assert r["success"] is False and "bootstrap step 0" in r["error"]


def test_post_as_get_poll_sends_a_body(monkeypatch):
    """Some transcript endpoints require POST with a body instead of GET."""
    import adapters.session_poll as sp
    polls, sent = [], {"done": False}
    def fake(method, url, **kw):
        if "/create" in url: return _Resp({"conversation_id": "c9"})
        if "/send" in url:
            sent["done"] = True
            return _Resp({"ok": True})
        polls.append((method, kw.get("json") or kw.get("data")))
        msgs = [{"role": "user", "text": "x"}]
        if sent["done"]:
            msgs.append({"role": "assistant", "text": "ok"})
        return _Resp({"messages": msgs})
    monkeypatch.setattr(sp.requests, "request", fake)
    cfg = {"create": {"url": "http://t/create", "extract": "conversation_id"},
           "send": {"url": "http://t/{{CONV}}/send"},
           "poll": {"url": "http://t/conversations/{{CONV}}", "method": "POST",
                    "body": {"conversation_id": "{{CONV}}"},
                    "interval_ms": 1, "timeout_ms": 500}}
    r = run_async(SessionPollAdapter().send_prompt("x", cfg))
    assert r["success"] is True
    assert polls and polls[0][0] == "POST"
    assert polls[0][1] == {"conversation_id": "c9"}

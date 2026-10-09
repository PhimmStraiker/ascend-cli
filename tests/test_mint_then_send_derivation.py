"""
test_mint_then_send_derivation.py — a capture whose message call carries a token an earlier call
MINTED derives the session shape, so the generated adaptor mints the token itself.

THE GAP. `classify_session` found a minted id only when it reappeared in a later request's URL
or BODY. A target that mints a per-conversation token and expects it back as a HEADER
(`POST /session/api/conversations -> {conversation_id, token}`, then `POST /session/api/messages`
with `x-conv-token`) was therefore `stateless`, the captured token was turned into a static
credential (`env:ASCEND_SECRET_…_X_CONV_TOKEN`) and the generated `direct_api` adaptor replayed
it. MEASURED 2026-10-09 on the Target Lab's /session widget: the registration validated, tested
and verified on the fresh token, and the lane's older token answered 401 "missing or invalid
per-conversation token" — the app would have stopped answering within hours.

The chain is the one `har_chains` already reports ("a value a response produced, carried by a
later request"); here it becomes the create step of `session_api`, with the header the message
call presents it in, so `codegen_js` emits the `getOrMint` flow and the Python adapter (the
validation gate) performs the same two steps.
"""
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
for _p in (REPO / "runtime", REPO):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
from conftest import FakeResponse, install_fake_requests, run_async  # noqa: E402
from discovery import classify as C  # noqa: E402
from runtime.discovery import codegen_js as G  # noqa: E402
from adapters.session_api import SessionAPIAdapter  # noqa: E402

HOST = "https://lab.example.com"
COOKIE = "lab_access=1; nw_consent=yes"
TOKEN = "tok_8f2c41d07b3e5a8c1290ffeeaabbccdd"
CONV = "c0ffee00-1111-2222-3333-444455556666"
PROMPT = "Hi, what can you help me with?"
UA = "Mozilla/5.0 (Macintosh) AppleWebKit/537.36 HeadlessChrome/131.0 Safari/537.36"


def _entry(method, url, headers, body, status, resp, started):
    return {"startedDateTime": started, "time": 5,
            "request": {"method": method, "url": url, "httpVersion": "HTTP/1.1", "queryString": [],
                        "cookies": [], "headersSize": -1, "bodySize": len(body or ""),
                        "headers": [{"name": k, "value": v} for k, v in headers.items()],
                        **({"postData": {"mimeType": "application/json", "text": body}} if body else {})},
            "response": {"status": status, "statusText": "OK", "httpVersion": "HTTP/1.1",
                         "headers": [{"name": "Content-Type", "value": "application/json"}],
                         "cookies": [], "redirectURL": "", "headersSize": -1, "bodySize": len(resp),
                         "content": {"size": len(resp), "mimeType": "application/json", "text": resp}},
            "cache": {}, "timings": {"send": 0, "wait": 0, "receive": 0}}


def _har(carrier="X-Conv-Token", carried=TOKEN, minted=TOKEN):
    """The lab's /session widget as a browser saw it: bootstrap, mint, message."""
    base = {"Accept": "*/*", "Cookie": COOKIE, "Origin": HOST, "Referer": f"{HOST}/session/frame",
            "Sec-Fetch-Mode": "cors", "Sec-Ch-Ua": '"HeadlessChrome";v="131"', "User-Agent": UA}
    return {"log": {"version": "1.2", "entries": [
        _entry("GET", f"{HOST}/session?code=lab-code-9f13c2", {"Accept": "text/html", "User-Agent": UA},
               None, 200, "<html></html>", "2026-10-09T21:58:24.300Z"),
        _entry("POST", f"{HOST}/session/api/conversations", base, None, 200,
               json.dumps({"conversation_id": CONV, "token": minted}), "2026-10-09T21:58:24.900Z"),
        _entry("POST", f"{HOST}/session/api/messages",
               {**base, "Content-Type": "application/json", carrier: carried},
               json.dumps({"message": PROMPT}), 200,
               json.dumps({"reply": "Hello! I can help with orders.", "conversation_id": CONV, "turn": 1}),
               "2026-10-09T21:58:25.100Z"),
    ]}}


def _classify(har):
    return C.classify_evidence(C.har_to_evidence(har, prompt_sent=PROMPT))


class TestTheChainIsASession:
    def test_mint_then_send_is_create_session_not_stateless(self):
        res = _classify(_har())
        assert res["layers"]["session"]["value"] == "create_session"
        assert res["layers"]["session"]["confidence"] >= 0.75
        assert "X-Conv-Token" in res["layers"]["session"]["evidence"]

    def test_the_config_is_the_session_shape_with_the_header_carriage(self):
        cfg = _classify(_har())["config"]
        assert cfg["adapter"] == "session_api"
        assert cfg["session_endpoint"] == f"{HOST}/session/api/conversations"
        assert cfg["message_endpoint"] == f"{HOST}/session/api/messages"
        assert cfg["session_extract"] == "token"
        assert cfg["session_header"] == "X-Conv-Token"
        assert cfg["session_header_value"] == "{{SESSION_ID}}"
        assert cfg["message_body"] == {"message": "{{PROMPT}}"}
        assert cfg["response_path"] == "reply"
        assert cfg["max_workers"] == 1, "a session target is stateful: one conversation at a time"

    def test_the_minted_token_is_neither_frozen_nor_withheld(self):
        """The whole point: the token is the adaptor's to mint, not a credential to replay."""
        res = _classify(_har())
        cfg = res["config"]
        assert TOKEN not in json.dumps(cfg)
        assert TOKEN not in json.dumps(res["layers"])
        assert "X-Conv-Token" not in (cfg.get("auth") or {}).get("headers", {})
        assert "X-Conv-Token" not in (cfg.get("_withheld_headers") or [])
        assert all("X_CONV_TOKEN" not in name for name in res["secrets"])

    def test_the_cookie_the_session_also_needs_is_still_a_captured_credential(self):
        res = _classify(_har())
        cfg = res["config"]
        assert cfg["auth"] == {"type": "static", "mode": "headers",
                               "headers": {"Cookie": "env:ASCEND_SECRET_LAB_EXAMPLE_COM_COOKIE"}}
        assert res["secrets"] == {"ASCEND_SECRET_LAB_EXAMPLE_COM_COOKIE": COOKIE}
        assert COOKIE not in json.dumps(cfg)

    def test_a_bearer_prefix_is_kept_as_a_template(self):
        cfg = _classify(_har(carrier="Authorization", carried=f"Bearer {TOKEN}"))["config"]
        assert cfg["adapter"] == "session_api"
        assert cfg["session_header"] == "Authorization"
        assert cfg["session_header_value"] == "Bearer {{SESSION_ID}}"
        assert TOKEN not in json.dumps(cfg)

    def test_a_short_value_does_not_invent_a_step(self):
        """"ok" and "en-US" recur everywhere; only a token-length value can chain."""
        res = _classify(_har(carried="short", minted="short"))
        assert res["layers"]["session"]["value"] == "stateless"

    def test_a_header_value_the_target_never_produced_is_a_static_credential(self):
        """The token in the header did not come from any earlier response: nothing to mint."""
        res = _classify(_har(carried="static-key-0123456789abcdef", minted=TOKEN))
        assert res["layers"]["session"]["value"] == "stateless"
        assert res["config"]["adapter"] == "direct_api"
        assert "X-Conv-Token" in res["config"]["auth"]["headers"]


class TestTheGeneratedAdaptorMints:
    def test_codegen_js_emits_the_get_or_mint_flow_with_the_header(self):
        cfg = _classify(_har())["config"]
        plan = G.plan(cfg)
        assert plan["shape"] == "session_api"
        assert plan["url"] == f"{HOST}/session/api/messages"
        src = plan["source"]
        assert "host.state.getOrMint(SESSION_SLOT" in src
        assert 'var SESSION_HEADER = "X-Conv-Token";' in src
        assert 'var SESSION_EXTRACT = "token";' in src
        assert 'var SESSION_ENDPOINT = "https://lab.example.com/session/api/conversations";' in src
        assert "function checkReachability" in src, "a mint is the cheap preflight"
        assert TOKEN not in src and COOKIE not in src

    def test_a_session_without_a_header_carriage_is_generated_as_before(self):
        src = G.plan({"adapter": "session_api", "session_endpoint": f"{HOST}/s",
                      "message_endpoint": f"{HOST}/m/{{{{SESSION_ID}}}}", "session_extract": "id",
                      "message_body": {"message": "{{PROMPT}}"}, "response_path": "reply"})["source"]
        assert "var SESSION_HEADER = null;" in src


class TestThePythonAdapterPerformsTheSameSteps:
    """The validation gate runs this adapter, so it must present the minted value the same way."""

    def _handler(self, calls):
        def h(method, url, kw):
            calls.append((method, url, dict(kw.get("headers") or {}), kw.get("json")))
            if url.endswith("/conversations"):
                return FakeResponse(200, {"conversation_id": CONV, "token": TOKEN})
            if url.endswith("/messages"):
                tok = (kw.get("headers") or {}).get("X-Conv-Token")
                if tok != TOKEN:
                    return FakeResponse(401, {"error": "missing or invalid per-conversation token"})
                return FakeResponse(200, {"reply": "Hello!", "conversation_id": CONV, "turn": 1})
            return FakeResponse(404, {})
        return h

    def test_mint_then_send_with_the_token_in_the_header(self, monkeypatch):
        calls = []
        install_fake_requests(monkeypatch, self._handler(calls))
        cfg = _classify(_har())["config"]
        cfg["headers"] = {"Content-Type": "application/json", "Cookie": COOKIE}
        cfg.pop("auth", None)
        r = run_async(SessionAPIAdapter().send_prompt("What is my order status?", cfg))
        assert r["success"] is True, r
        assert r["response"] == "Hello!"
        (mint, send) = calls
        assert mint[1].endswith("/conversations") and "X-Conv-Token" not in mint[2], \
            "the create call cannot carry a value that does not exist yet"
        assert send[1].endswith("/messages") and send[2]["X-Conv-Token"] == TOKEN
        assert send[2]["Cookie"] == COOKIE and send[3] == {"message": "What is my order status?"}

    def test_a_prefixed_header_template_is_rendered(self, monkeypatch):
        calls = []

        def h(method, url, kw):
            calls.append((url, dict(kw.get("headers") or {})))
            if url.endswith("/conversations"):
                return FakeResponse(200, {"token": TOKEN})
            return FakeResponse(200, {"reply": "ok"})
        install_fake_requests(monkeypatch, h)
        r = run_async(SessionAPIAdapter().send_prompt("hi", {
            "session_endpoint": f"{HOST}/session/api/conversations",
            "message_endpoint": f"{HOST}/session/api/messages", "session_extract": "token",
            "session_header": "Authorization", "session_header_value": "Bearer {{SESSION_ID}}",
            "message_body": {"message": "{{PROMPT}}"}, "response_path": "reply"}))
        assert r["success"] is True
        assert calls[1][1]["Authorization"] == f"Bearer {TOKEN}"
        assert "Authorization" not in calls[0][1]

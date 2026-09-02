"""
test_adapters_config.py — every adapter honours the BotAdapter result contract.

For all 15 registered adapters we exercise send_prompt offline and assert the
return shape is always {response, success, error, duration_ms, metadata} with the
right types — on the happy path, on a missing-config fast-fail, and on a transport
error. An adapter must NEVER raise out of send_prompt (a raised probe would be
dropped); it must return a well-formed failure the router can shape into a result.

Transport is mocked per adapter family: requests.{request,post,get} for the REST
adapters, requests.Session for the streaming SSE adapter, urllib for the Slack /
SCRT2 adapters, and websockets.connect for the WebSocket adapter. No sockets.
"""
import importlib
import json
import urllib.error
import urllib.request

import pytest

from conftest import FakeResponse, install_fake_requests, run_async

dispatch = importlib.import_module("dispatch")
import requests  # noqa: E402

ALL_ADAPTERS = sorted(dispatch.ADAPTER_REGISTRY)


# --------------------------------------------------------------------------- #
# contract helper
# --------------------------------------------------------------------------- #
CONTRACT_KEYS = {"response", "success", "error", "duration_ms", "metadata"}


def assert_contract(r):
    assert isinstance(r, dict)
    assert CONTRACT_KEYS.issubset(r), f"missing keys: {CONTRACT_KEYS - set(r)}"
    assert isinstance(r["response"], str)
    assert isinstance(r["success"], bool)
    assert isinstance(r["duration_ms"], int)
    assert isinstance(r["metadata"], dict)
    if r["success"]:
        assert r["error"] is None
    else:
        assert r["response"] == ""
        assert r["error"]  # some non-empty message


def new(adapter_name):
    return dispatch.ADAPTER_REGISTRY[adapter_name]()


# --------------------------------------------------------------------------- #
# 1) Missing-config fast-fail returns a well-formed failure for EVERY adapter
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", ALL_ADAPTERS)
def test_empty_config_fails_cleanly(name):
    r = run_async(new(name).send_prompt("hello", {}))
    assert_contract(r)
    assert r["success"] is False


@pytest.mark.parametrize("name", ALL_ADAPTERS)
def test_empty_config_does_not_raise(name):
    # send_prompt must swallow its own errors; a raise here would drop a probe
    try:
        r = run_async(new(name).send_prompt("probe", {}))
    except Exception as e:  # noqa: BLE001
        pytest.fail(f"{name}.send_prompt raised {type(e).__name__}: {e}")
    assert_contract(r)


# --------------------------------------------------------------------------- #
# 2) direct_api — success + rich config permutations
# --------------------------------------------------------------------------- #
def _direct(json_data=None, status=200, text=None, not_json=False):
    def handler(method, url, kwargs):
        return FakeResponse(status, json_data, text=text, not_json=not_json)
    return handler


def test_direct_api_success_simple(monkeypatch):
    install_fake_requests(monkeypatch, _direct({"response": "hi there"}))
    r = run_async(new("direct_api").send_prompt("x", {
        "endpoint": "https://t/api", "body": {"message": "{{PROMPT}}"}}))
    assert_contract(r)
    assert r["success"] is True
    assert r["response"] == "hi there"


@pytest.mark.parametrize("path,data,expected", [
    ("choices.0.message.content", {"choices": [{"message": {"content": "A"}}]}, "A"),
    ("reply", {"reply": "B"}, "B"),
    ("data.text", {"data": {"text": "C"}}, "C"),
    ("out.0", {"out": ["D", "E"]}, "D"),
    ("deep.nested.value", {"deep": {"nested": {"value": "F"}}}, "F"),
])
def test_direct_api_response_path_extraction(monkeypatch, path, data, expected):
    install_fake_requests(monkeypatch, _direct(data))
    r = run_async(new("direct_api").send_prompt("x", {
        "endpoint": "https://t/api", "body": {"message": "{{PROMPT}}"},
        "response_path": path}))
    assert r["success"] is True
    assert r["response"] == expected


@pytest.mark.parametrize("method", ["POST", "GET", "PUT", "post", "get"])
def test_direct_api_methods(monkeypatch, method):
    rec = install_fake_requests(monkeypatch, _direct({"response": "ok"}))
    r = run_async(new("direct_api").send_prompt("x", {
        "endpoint": "https://t/api", "method": method,
        "body": {"message": "{{PROMPT}}"}}))
    assert r["success"] is True
    assert rec.last["method"] == method.upper()


@pytest.mark.parametrize("timeout_ms", [1000, 5000, 30000, 60000])
def test_direct_api_timeout_passthrough(monkeypatch, timeout_ms):
    rec = install_fake_requests(monkeypatch, _direct({"response": "ok"}))
    run_async(new("direct_api").send_prompt("x", {
        "endpoint": "https://t/api", "body": {"message": "{{PROMPT}}"},
        "timeout_ms": timeout_ms}))
    assert rec.last["kwargs"]["timeout"] == timeout_ms / 1000


def test_direct_api_missing_endpoint(monkeypatch):
    r = run_async(new("direct_api").send_prompt("x", {"body": {"message": "{{PROMPT}}"}}))
    assert_contract(r)
    assert r["success"] is False
    assert "endpoint" in r["error"].lower()


def test_direct_api_bad_response_path(monkeypatch):
    install_fake_requests(monkeypatch, _direct({"response": "ok"}))
    r = run_async(new("direct_api").send_prompt("x", {
        "endpoint": "https://t/api", "body": {"message": "{{PROMPT}}"},
        "response_path": "does.not.exist"}))
    assert_contract(r)
    assert r["success"] is False


def test_direct_api_non_json_response_no_path_is_text(monkeypatch):
    # Plain-text bots: non-JSON body with NO response_path -> the raw text IS the answer.
    install_fake_requests(monkeypatch, _direct(text="just plain text reply", not_json=True))
    r = run_async(new("direct_api").send_prompt("x", {
        "endpoint": "https://t/api", "body": {"message": "{{PROMPT}}"}}))
    assert_contract(r)
    assert r["success"] is True
    assert r["response"] == "just plain text reply"


def test_direct_api_non_json_with_path_fails(monkeypatch):
    # A response_path demands JSON; non-JSON must fail cleanly, not silently pass.
    install_fake_requests(monkeypatch, _direct(text="<html>not json</html>", not_json=True))
    r = run_async(new("direct_api").send_prompt("x", {
        "endpoint": "https://t/api", "body": {"message": "{{PROMPT}}"},
        "response_path": "data.reply"}))
    assert_contract(r)
    assert r["success"] is False


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429, 500, 503])
def test_direct_api_http_error(monkeypatch, status):
    install_fake_requests(monkeypatch, _direct({"x": 1}, status=status))
    r = run_async(new("direct_api").send_prompt("x", {
        "endpoint": "https://t/api", "body": {"message": "{{PROMPT}}"}}))
    assert_contract(r)
    assert r["success"] is False


def test_direct_api_connection_error(monkeypatch):
    def handler(method, url, kwargs):
        raise requests.ConnectionError("refused")
    install_fake_requests(monkeypatch, handler)
    r = run_async(new("direct_api").send_prompt("x", {
        "endpoint": "https://t/api", "body": {"message": "{{PROMPT}}"}}))
    assert_contract(r)
    assert r["success"] is False


# --------------------------------------------------------------------------- #
# 3) session_api — success + failures
# --------------------------------------------------------------------------- #
def _session_handler(session_data, message_data, s_status=200, m_status=200):
    def handler(method, url, kwargs):
        if url.rstrip("/").endswith("session") or "/session" in url and "message" not in url and "send" not in url:
            return FakeResponse(s_status, session_data)
        return FakeResponse(m_status, message_data)
    return handler


def test_session_api_success(monkeypatch):
    install_fake_requests(monkeypatch, _session_handler(
        {"sessionId": "S1"}, {"messages": [{"message": "reply"}]}))
    r = run_async(new("session_api").send_prompt("hi", {
        "session_endpoint": "https://t/session",
        "message_endpoint": "https://t/send",
        "message_body": {"text": "{{PROMPT}}"}}))
    assert_contract(r)
    assert r["success"] is True
    assert r["response"] == "reply"


def test_session_api_missing_endpoints(monkeypatch):
    r = run_async(new("session_api").send_prompt("hi", {"session_endpoint": "https://t/s"}))
    assert_contract(r)
    assert r["success"] is False


def test_session_api_no_session_id(monkeypatch):
    install_fake_requests(monkeypatch, _session_handler({"wrong": "x"}, {}))
    r = run_async(new("session_api").send_prompt("hi", {
        "session_endpoint": "https://t/session",
        "message_endpoint": "https://t/send",
        "message_body": {"text": "{{PROMPT}}"}}))
    assert_contract(r)
    assert r["success"] is False


def test_session_api_session_creation_http_error(monkeypatch):
    install_fake_requests(monkeypatch, _session_handler({}, {}, s_status=500))
    r = run_async(new("session_api").send_prompt("hi", {
        "session_endpoint": "https://t/session",
        "message_endpoint": "https://t/send",
        "message_body": {"text": "{{PROMPT}}"}}))
    assert_contract(r)
    assert r["success"] is False


# --------------------------------------------------------------------------- #
# 4) vertex_ai — mocked ADC token + streamQuery body
# --------------------------------------------------------------------------- #
def test_vertex_success(monkeypatch):
    adapter = new("vertex_ai")
    monkeypatch.setattr(adapter, "_token", lambda config=None: "adc-token")
    body = json.dumps([{"content": {"parts": [{"text": "hello "}]}},
                       {"content": {"parts": [{"text": "world"}]}}])
    install_fake_requests(monkeypatch, lambda m, u, k: FakeResponse(200, text=body))
    r = run_async(adapter.send_prompt("x", {"endpoint": "https://vertex/agent:streamQuery"}))
    assert_contract(r)
    assert r["success"] is True
    assert r["response"] == "hello world"


def test_vertex_token_error(monkeypatch):
    adapter = new("vertex_ai")

    def boom(config=None):
        raise RuntimeError("no ADC")
    monkeypatch.setattr(adapter, "_token", boom)
    r = run_async(adapter.send_prompt("x", {"endpoint": "https://vertex/x"}))
    assert_contract(r)
    assert r["success"] is False
    assert "ADC" in r["error"] or "token" in r["error"].lower()


def test_vertex_missing_endpoint():
    r = run_async(new("vertex_ai").send_prompt("x", {}))
    assert_contract(r)
    assert r["success"] is False


def test_vertex_http_error(monkeypatch):
    adapter = new("vertex_ai")
    monkeypatch.setattr(adapter, "_token", lambda: "t")
    install_fake_requests(monkeypatch, lambda m, u, k: FakeResponse(500, text="err"))
    r = run_async(adapter.send_prompt("x", {"endpoint": "https://vertex/x"}))
    assert_contract(r)
    assert r["success"] is False


# --------------------------------------------------------------------------- #
# 5) websocket_direct — mocked websockets.connect
# --------------------------------------------------------------------------- #
class _FakeWS:
    def __init__(self, frames):
        self.frames = list(frames)
        self.sent = []

    async def send(self, m):
        self.sent.append(m)

    async def recv(self):
        if self.frames:
            return self.frames.pop(0)
        raise ConnectionError("closed")


class _FakeConnect:
    def __init__(self, ws):
        self.ws = ws

    async def __aenter__(self):
        return self.ws

    async def __aexit__(self, *exc):
        return False


def test_websocket_success(monkeypatch):
    import websockets
    ws = _FakeWS(['{"text": "hello "}', '{"text": "world"}'])
    monkeypatch.setattr(websockets, "connect", lambda url, **kw: _FakeConnect(ws))
    r = run_async(new("websocket_direct").send_prompt("probe", {
        "ws_url": "wss://t/socket", "idle_ms": 100}))
    assert_contract(r)
    assert r["success"] is True
    assert r["response"] == "hello world"
    assert ws.sent  # the prompt frame was sent


def test_websocket_prompt_in_frame(monkeypatch):
    import websockets
    ws = _FakeWS(['{"text": "ok"}'])
    monkeypatch.setattr(websockets, "connect", lambda url, **kw: _FakeConnect(ws))
    run_async(new("websocket_direct").send_prompt('pay"load', {
        "ws_url": "wss://t/socket", "idle_ms": 100,
        "send_template": {"type": "msg", "text": "{{PROMPT}}"}}))
    sent = json.loads(ws.sent[-1])
    assert sent["text"] == 'pay"load'
    assert set(sent.keys()) == {"type", "text"}


def test_websocket_missing_url():
    r = run_async(new("websocket_direct").send_prompt("x", {}))
    assert_contract(r)
    assert r["success"] is False


def test_websocket_connect_raises(monkeypatch):
    import websockets

    def boom(url, **kw):
        raise OSError("handshake failed")
    monkeypatch.setattr(websockets, "connect", boom)
    r = run_async(new("websocket_direct").send_prompt("x", {"ws_url": "wss://t/x"}))
    assert_contract(r)
    assert r["success"] is False


# --------------------------------------------------------------------------- #
# 6) slack_direct — mocked urllib
# --------------------------------------------------------------------------- #
def test_slack_missing_config():
    r = run_async(new("slack_direct").send_prompt("x", {}))
    assert_contract(r)
    assert r["success"] is False


def test_slack_transport_error(monkeypatch):
    def boom(req, timeout=None, **kw):
        raise urllib.error.URLError("no route to slack")
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    r = run_async(new("slack_direct").send_prompt("x", {
        "slack_token": "xoxp-1", "channel_id": "D1", "user_id": "U1",
        "timeout_ms": 1000, "poll_interval_ms": 10}))
    assert_contract(r)
    assert r["success"] is False


# --------------------------------------------------------------------------- #
# 7) scrt2_direct — mocked urllib
# --------------------------------------------------------------------------- #
def test_scrt2_missing_config():
    r = run_async(new("scrt2_direct").send_prompt("x", {}))
    assert_contract(r)
    assert r["success"] is False


def test_scrt2_transport_error(monkeypatch):
    def boom(req, timeout=None, **kw):
        raise urllib.error.URLError("scrt2 unreachable")
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    r = run_async(new("scrt2_direct").send_prompt("x", {
        "scrt_base": "https://scrt", "org_id": "00D", "developer_name": "Bot",
        "widget_origin": "https://site"}))
    assert_contract(r)
    assert r["success"] is False


# --------------------------------------------------------------------------- #
# 8) amazon_connect — mocked requests
# --------------------------------------------------------------------------- #
def test_amazon_connect_missing_config():
    r = run_async(new("amazon_connect").send_prompt("x", {}))
    assert_contract(r)
    assert r["success"] is False


def test_amazon_connect_transport_error(monkeypatch):
    def handler(method, url, kwargs):
        raise requests.ConnectionError("connect down")
    install_fake_requests(monkeypatch, handler)
    r = run_async(new("amazon_connect").send_prompt("x", {
        "token_endpoint": "https://t/token", "start_endpoint": "https://t/start"}))
    assert_contract(r)
    assert r["success"] is False


# --------------------------------------------------------------------------- #
# 9) agentforce — auth fast-fail (no client credentials)
# --------------------------------------------------------------------------- #
def test_agentforce_missing_config():
    r = run_async(new("agentforce").send_prompt("x", {}))
    assert_contract(r)
    assert r["success"] is False


def test_agentforce_missing_credentials(monkeypatch):
    # instance_url + agent_id present, but no client_id/secret → auth error, no net
    r = run_async(new("agentforce").send_prompt("x", {
        "instance_url": "https://sf.example", "agent_id": "0Xx"}))
    assert_contract(r)
    assert r["success"] is False


def test_agentforce_token_http_error(monkeypatch):
    def handler(method, url, kwargs):
        return FakeResponse(400, text="invalid_client")
    install_fake_requests(monkeypatch, handler)
    r = run_async(new("agentforce").send_prompt("x", {
        "instance_url": "https://sf.example", "agent_id": "0Xx",
        "client_id": "cid", "client_secret": "sec"}))
    assert_contract(r)
    assert r["success"] is False


# --------------------------------------------------------------------------- #
# 10) copilot_studio — no token source → graceful fail
# --------------------------------------------------------------------------- #
def test_copilot_missing_token_source(monkeypatch):
    # empty config → no directline token source; must fail cleanly, no network
    r = run_async(new("copilot_studio").send_prompt("x", {}))
    assert_contract(r)
    assert r["success"] is False


def test_copilot_token_endpoint_error(monkeypatch):
    def handler(method, url, kwargs):
        raise requests.ConnectionError("directline down")
    install_fake_requests(monkeypatch, handler)
    r = run_async(new("copilot_studio").send_prompt("x", {
        "directline_token_endpoint": "https://dl/token"}))
    assert_contract(r)
    assert r["success"] is False


# --------------------------------------------------------------------------- #
# 11) sse_stream — mocked requests.Session
# --------------------------------------------------------------------------- #
class _FakeSession:
    def __init__(self, raise_exc=None):
        self.headers = {}
        self.cookies = {}
        self._raise = raise_exc

    def request(self, *a, **kw):
        if self._raise:
            raise self._raise
        raise requests.ConnectionError("no response configured")

    def get(self, *a, **kw):
        raise requests.ConnectionError("down")

    def post(self, *a, **kw):
        raise requests.ConnectionError("down")

    def close(self):
        pass


def test_sse_missing_config():
    r = run_async(new("sse_stream").send_prompt("x", {}))
    assert_contract(r)
    assert r["success"] is False


def test_sse_transport_error(monkeypatch):
    monkeypatch.setattr(requests, "Session",
                        lambda: _FakeSession(requests.ConnectionError("sse down")))
    r = run_async(new("sse_stream").send_prompt("x", {
        "base_url": "https://t", "chat_path": "/chat"}))
    assert_contract(r)
    assert r["success"] is False


# --------------------------------------------------------------------------- #
# 12) browser — playwright not exercised; missing url fast-fail
# --------------------------------------------------------------------------- #
def test_browser_missing_url():
    r = run_async(new("browser").send_prompt("x", {}))
    assert_contract(r)
    assert r["success"] is False

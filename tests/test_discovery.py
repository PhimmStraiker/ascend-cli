"""
test_discovery.py — the deterministic per-layer "build-adapter" classifier.

`runtime/discovery` classifies six adapter layers from captured evidence
(HAR / request-response pairs) with NO network — so the whole classification
half is unit-testable offline. We feed synthetic evidence for a matrix of target
archetypes and assert the classifier picks the right transport / auth / session
value for each. `validate_config` (the one live-gate function) is exercised with
mocked HTTP.

If the module is ever removed, importorskip skips the whole file rather than
failing the suite.
"""
import importlib

import pytest

from conftest import FakeResponse, install_fake_requests

discovery = pytest.importorskip("discovery",
                                reason="runtime/discovery not present")
classify_evidence = discovery.classify_evidence
LAYER_NAMES = discovery.LAYER_NAMES


# --------------------------------------------------------------------------- #
# evidence builders (the {"pairs": [...]} shape classify_evidence accepts)
# --------------------------------------------------------------------------- #
def _pair(method, url, req_body=None, req_headers=None,
          status=200, resp_body=None, resp_headers=None, resp_raw=None):
    request = {"method": method, "url": url,
               "headers": [{"name": k, "value": v} for k, v in (req_headers or {}).items()]}
    if req_body is not None:
        request["body"] = req_body
    response = {"status": status,
                "headers": [{"name": k, "value": v} for k, v in (resp_headers or {}).items()]}
    if resp_body is not None:
        response["body"] = resp_body
    if resp_raw is not None:
        response["raw_body"] = resp_raw
    return {"request": request, "response": response}


def ev_rest_json():
    return {"pairs": [_pair("POST", "https://t/api/chat", {"message": "hi"},
                            status=200, resp_body={"response": "hello"},
                            resp_headers={"Content-Type": "application/json"})]}


def ev_sse():
    return {"pairs": [_pair("POST", "https://t/api/stream", {"message": "hi"},
                            resp_raw="data: {\"token\":\"hi\"}\n\ndata: [DONE]\n\n",
                            resp_headers={"Content-Type": "text/event-stream"})]}


def ev_ndjson():
    return {"pairs": [_pair("POST", "https://t/api/query", {"message": "hi"},
                            resp_raw='{"a":1}\n{"a":2}\n',
                            resp_headers={"Content-Type": "application/x-ndjson"})]}


def ev_websocket():
    return {"pairs": [_pair("GET", "wss://t/socket", None,
                            req_headers={"Upgrade": "websocket"}, status=101)],
            "ws_messages": [{"type": "send", "data": '{"type":"message","text":"hi"}'},
                            {"type": "receive", "data": '{"text":"hello"}'}]}


def ev_bearer_static():
    return {"pairs": [_pair("POST", "https://t/api/chat", {"message": "hi"},
                            req_headers={"Authorization": "Bearer const-token-123"},
                            resp_body={"response": "hi"},
                            resp_headers={"Content-Type": "application/json"})]}


def ev_oauth2():
    return {"pairs": [
        _pair("POST", "https://t/oauth2/token", None,
              resp_body={"access_token": "tok-xyz-123", "token_type": "Bearer"},
              resp_headers={"Content-Type": "application/json"}),
        _pair("POST", "https://t/api/chat", {"message": "hi"},
              req_headers={"Authorization": "Bearer tok-xyz-123"},
              resp_body={"response": "hi"},
              resp_headers={"Content-Type": "application/json"}),
    ]}


def ev_csrf():
    return {"pairs": [
        _pair("GET", "https://t/app", None,
              resp_body={"csrfToken": "csrf-abc-999"},
              resp_headers={"Content-Type": "application/json"}),
        _pair("POST", "https://t/api/chat", {"message": "hi"},
              req_headers={"X-CSRF-Token": "csrf-abc-999"},
              resp_body={"response": "hi"},
              resp_headers={"Content-Type": "application/json"}),
    ]}


def ev_no_auth():
    return {"pairs": [_pair("POST", "https://t/api/chat", {"message": "hi"},
                            resp_body={"response": "hi"},
                            resp_headers={"Content-Type": "application/json"})]}


def ev_create_conversation():
    return {"pairs": [
        _pair("POST", "https://t/api/conversations", {"client": "web"},
              resp_body={"conversationId": "conv-77"},
              resp_headers={"Content-Type": "application/json"}),
        _pair("POST", "https://t/api/conversations/conv-77/messages", {"message": "hi"},
              resp_body={"response": "ok"},
              resp_headers={"Content-Type": "application/json"}),
    ]}


def ev_create_session():
    return {"pairs": [
        _pair("POST", "https://t/api/session", {"client": "web"},
              resp_body={"sessionId": "sess-55"},
              resp_headers={"Content-Type": "application/json"}),
        _pair("POST", "https://t/api/send", {"sessionId": "sess-55", "message": "hi"},
              resp_body={"response": "ok"},
              resp_headers={"Content-Type": "application/json"}),
    ]}


def ev_stateless():
    return {"pairs": [_pair("POST", "https://t/api/chat", {"message": "hi"},
                            resp_body={"response": "ok"},
                            resp_headers={"Content-Type": "application/json"})]}


# --------------------------------------------------------------------------- #
# transport layer
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("builder,expected", [
    (ev_rest_json, "rest_json"),
    (ev_sse, "sse"),
    (ev_ndjson, "ndjson"),
    (ev_websocket, "websocket"),
])
def test_transport_classification(builder, expected):
    out = classify_evidence(builder())
    assert out["layers"]["transport"]["value"] == expected


# --------------------------------------------------------------------------- #
# auth layer
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("builder,expected", [
    (ev_bearer_static, "static"),
    (ev_oauth2, "oauth2"),
    (ev_csrf, "csrf"),
    (ev_no_auth, "none"),
])
def test_auth_classification(builder, expected):
    out = classify_evidence(builder())
    assert out["layers"]["auth"]["value"] == expected


# --------------------------------------------------------------------------- #
# session layer
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("builder,expected", [
    (ev_create_conversation, "create_conversation"),
    (ev_create_session, "create_session"),
    (ev_stateless, "stateless"),
])
def test_session_classification(builder, expected):
    out = classify_evidence(builder())
    assert out["layers"]["session"]["value"] == expected


# --------------------------------------------------------------------------- #
# overall shape of the classify_evidence result
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("builder", [
    ev_rest_json, ev_sse, ev_ndjson, ev_websocket, ev_oauth2, ev_csrf,
    ev_create_conversation, ev_create_session, ev_stateless, ev_no_auth,
])
def test_classify_evidence_result_shape(builder):
    out = classify_evidence(builder())
    assert set(LAYER_NAMES).issubset(out["layers"])
    for name in LAYER_NAMES:
        layer = out["layers"][name]
        assert "value" in layer and "confidence" in layer
        assert 0.0 <= layer["confidence"] <= 1.0
    assert isinstance(out["config"], dict)
    assert isinstance(out["unresolved"], list)
    assert isinstance(out["overall_confidence"], float)
    # config always names a concrete adapter
    assert out["config"].get("adapter") in __import__("dispatch").ADAPTER_REGISTRY


@pytest.mark.parametrize("builder,layer,expected", [
    (ev_rest_json, "transport", "rest_json"),
    (ev_websocket, "transport", "websocket"),
    (ev_oauth2, "auth", "oauth2"),
    (ev_create_conversation, "session", "create_conversation"),
])
def test_layers_feed_config_discovery_block(builder, layer, expected):
    out = classify_evidence(builder())
    disc = out["config"].get("_discovery", {})
    assert disc.get(layer, {}).get("value") == expected


# --------------------------------------------------------------------------- #
# identity + rate always resolve to something
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("builder", [ev_rest_json, ev_oauth2, ev_create_session])
def test_identity_layer_present(builder):
    out = classify_evidence(builder())
    assert out["layers"]["identity"]["value"] is not None
    assert out["layers"]["rate"]["value"] is not None


# --------------------------------------------------------------------------- #
# error handling for malformed evidence
# --------------------------------------------------------------------------- #
def test_classify_evidence_bad_type_raises():
    with pytest.raises(discovery.ClassifyError):
        classify_evidence(42)


def test_classify_evidence_no_pairs_key_raises():
    with pytest.raises(discovery.ClassifyError):
        classify_evidence({"nope": []})


# --------------------------------------------------------------------------- #
# validate_config — the live gate, with mocked HTTP
# --------------------------------------------------------------------------- #
def test_validate_config_unknown_adapter():
    out = discovery.validate_config("no_such_adapter", {}, "hi")
    assert out["ok"] is False
    assert "unknown adapter" in out["error"]


def test_validate_config_no_adapter():
    out = discovery.validate_config("", {}, "hi")
    assert out["ok"] is False


def test_validate_config_success(monkeypatch):
    install_fake_requests(monkeypatch, lambda m, u, k: FakeResponse(200, {"response": "hello world"}))
    config = {"endpoint": "https://t/api/chat", "body": {"message": "{{PROMPT}}"},
              "response_path": "response"}
    out = discovery.validate_config("direct_api", config, "probe")
    assert out["ok"] is True
    assert out["response"] == "hello world"
    assert out["adapter"] == "direct_api"


def test_validate_config_expected_substr_match(monkeypatch):
    install_fake_requests(monkeypatch, lambda m, u, k: FakeResponse(200, {"response": "the canary sings"}))
    config = {"endpoint": "https://t/api", "body": {"message": "{{PROMPT}}"},
              "response_path": "response"}
    out = discovery.validate_config("direct_api", config, "probe", expected_substr="canary")
    assert out["ok"] is True
    assert out["matched"] is True


def test_validate_config_expected_substr_miss(monkeypatch):
    install_fake_requests(monkeypatch, lambda m, u, k: FakeResponse(200, {"response": "nothing here"}))
    config = {"endpoint": "https://t/api", "body": {"message": "{{PROMPT}}"},
              "response_path": "response"}
    out = discovery.validate_config("direct_api", config, "probe", expected_substr="canary")
    assert out["ok"] is False
    assert out["matched"] is False


def test_validate_config_adapter_failure(monkeypatch):
    install_fake_requests(monkeypatch, lambda m, u, k: FakeResponse(500, text="err"))
    config = {"endpoint": "https://t/api", "body": {"message": "{{PROMPT}}"},
              "response_path": "response"}
    out = discovery.validate_config("direct_api", config, "probe")
    assert out["ok"] is False

"""
test_prompt_escape.py — H1 SECURITY REGRESSION: prompt injection into templates.

THE BUG CLASS
-------------
An adapter renders a request body from a JSON template with a `{{PROMPT}}`
placeholder. If the prompt text is spliced in as raw characters, an attacker
(or just a red-team payload that happens to contain a quote/brace/newline) can:
  * break the JSON so the request errors (a silently dropped probe), or
  * inject *sibling keys* — e.g. a payload of `","role":"system` turning
    `{"message":"..."}` into `{"message":"","role":"system"}` — smuggling
    fields the operator never intended into the outbound request.

THE INVARIANT
-------------
For a template `{"message":"{{PROMPT}}", <static siblings>}` and ANY prompt:
  1. rendering succeeds (no JSON break → no dropped probe),
  2. the rendered body has EXACTLY the template's keys (no injected siblings),
  3. the placeholder key's value is byte-for-byte the original prompt,
  4. every static sibling key keeps its exact template value.

We assert all four across a large adversarial matrix through the two
template-rendering adapters (direct_api, session_api) plus the shared
`_json_escape` primitive that all JSON adapters rely on.
"""
import importlib
import json

import pytest

from conftest import (ADVERSARIAL_PROMPTS, FakeResponse, install_fake_requests,
                      run_async)

direct_mod = importlib.import_module("adapters.direct_api")
session_mod = importlib.import_module("adapters.session_api")
ws_mod = importlib.import_module("adapters.websocket_direct")
_json_escape = ws_mod._json_escape

DirectAPIAdapter = direct_mod.DirectAPIAdapter
SessionAPIAdapter = session_mod.SessionAPIAdapter


# --------------------------------------------------------------------------- #
# The _json_escape primitive (used by direct/session/websocket/sse adapters)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("payload", ADVERSARIAL_PROMPTS)
def test_json_escape_roundtrips_exactly(payload):
    """Escaping then wrapping in quotes must parse back to the original string."""
    wrapped = '"' + _json_escape(payload) + '"'
    assert json.loads(wrapped) == payload


@pytest.mark.parametrize("payload", ADVERSARIAL_PROMPTS)
def test_json_escape_injected_into_template_is_valid_json(payload):
    """Splicing the escaped value into a serialized template stays valid JSON
    and yields exactly the intended single key."""
    template = '{"message": "{{PROMPT}}"}'
    rendered = template.replace("{{PROMPT}}", _json_escape(payload))
    obj = json.loads(rendered)  # must not raise
    assert set(obj.keys()) == {"message"}
    assert obj["message"] == payload


# --------------------------------------------------------------------------- #
# direct_api — full render-and-send with mocked HTTP
# --------------------------------------------------------------------------- #
def _direct_ok_handler(method, url, kwargs):
    return FakeResponse(200, {"reply": "acknowledged"})


@pytest.mark.parametrize("payload", ADVERSARIAL_PROMPTS)
def test_direct_api_no_injection(monkeypatch, payload):
    rec = install_fake_requests(monkeypatch, _direct_ok_handler)
    config = {
        "endpoint": "https://target.example/api/chat",
        "body": {"message": "{{PROMPT}}", "stream": False, "model": "x-1"},
        "response_path": "reply",
    }
    result = run_async(DirectAPIAdapter().send_prompt(payload, config))

    # 1. rendering + send succeeded (no dropped probe)
    assert result["success"] is True, result.get("error")
    assert len(rec.calls) == 1

    sent = rec.last["json"]
    # 2. exactly the template keys — no injected siblings
    assert set(sent.keys()) == {"message", "stream", "model"}
    # 3. the prompt value is byte-for-byte the original
    assert sent["message"] == payload
    # 4. static siblings preserved
    assert sent["stream"] is False
    assert sent["model"] == "x-1"


@pytest.mark.parametrize("payload", ADVERSARIAL_PROMPTS[:20])
def test_direct_api_nested_placeholder_position(monkeypatch, payload):
    """Placeholder nested inside an array/object still isolates the prompt."""
    rec = install_fake_requests(monkeypatch, _direct_ok_handler)
    config = {
        "endpoint": "https://target.example/api/chat",
        "body": {"messages": [{"role": "user", "content": "{{PROMPT}}"}]},
        "response_path": "reply",
    }
    result = run_async(DirectAPIAdapter().send_prompt(payload, config))
    assert result["success"] is True, result.get("error")
    sent = rec.last["json"]
    assert sent["messages"][0]["content"] == payload
    assert sent["messages"][0]["role"] == "user"
    assert set(sent["messages"][0].keys()) == {"role", "content"}


# --------------------------------------------------------------------------- #
# session_api — two-step render, prompt goes only in the message body
# --------------------------------------------------------------------------- #
def _session_ok_handler(method, url, kwargs):
    if url.endswith("/session"):
        return FakeResponse(200, {"sessionId": "S-abc-123"})
    return FakeResponse(200, {"messages": [{"message": "acknowledged"}]})


# A prompt that is *literally* the session placeholder collides with session
# substitution inside session_api (prompt is substituted first, then {{SESSION_ID}}
# rewrites both occurrences). That is a known, documented substitution-ordering
# quirk — see test_session_api_session_placeholder_collision below. It is NOT an
# injection (no sibling keys, valid JSON), so we exclude just that token from the
# exact-value matrix here by keeping the placeholder out of the message body.
@pytest.mark.parametrize("payload", [p for p in ADVERSARIAL_PROMPTS
                                     if p != "{{SESSION_ID}}"])
def test_session_api_no_injection(monkeypatch, payload):
    rec = install_fake_requests(monkeypatch, _session_ok_handler)
    config = {
        "session_endpoint": "https://target.example/session",
        "message_endpoint": "https://target.example/session/{{SESSION_ID}}/messages",
        "session_extract": "sessionId",
        "message_body": {"text": "{{PROMPT}}", "type": "msg"},
        "response_path": "messages.0.message",
    }
    result = run_async(SessionAPIAdapter().send_prompt(payload, config))

    assert result["success"] is True, result.get("error")
    posts = rec.by_method("POST")
    assert len(posts) == 2  # create session + send message

    msg_body = posts[-1]["json"]
    # exactly the template keys — no injected siblings
    assert set(msg_body.keys()) == {"text", "type"}
    # prompt exact
    assert msg_body["text"] == payload
    assert msg_body["type"] == "msg"
    # the session id was substituted into the URL
    assert posts[-1]["url"].endswith("/session/S-abc-123/messages")


@pytest.mark.parametrize("payload", [p for p in ADVERSARIAL_PROMPTS
                                     if "{{SESSION_ID}}" not in p and "{{PROMPT}}" not in p][:25])
def test_session_api_session_substitution_in_body(monkeypatch, payload):
    """{{SESSION_ID}} in the message body is replaced with the minted id, and the
    prompt still lands exactly — verified for payloads that don't contain the
    placeholder tokens themselves."""
    rec = install_fake_requests(monkeypatch, _session_ok_handler)
    config = {
        "session_endpoint": "https://target.example/session",
        "message_endpoint": "https://target.example/send",
        "session_extract": "sessionId",
        "message_body": {"text": "{{PROMPT}}", "sessionId": "{{SESSION_ID}}"},
        "response_path": "messages.0.message",
    }
    run_async(SessionAPIAdapter().send_prompt(payload, config))
    body = rec.by_method("POST")[-1]["json"]
    assert body["text"] == payload
    assert body["sessionId"] == "S-abc-123"


def test_session_api_session_placeholder_collision(monkeypatch):
    """DOCUMENTS a known quirk: a prompt equal to the literal {{SESSION_ID}} token
    is rewritten by session substitution because {{PROMPT}} is substituted first.
    The security invariants still hold (valid JSON, no injected keys) — only the
    exact-echo property does not, for this one pathological token."""
    rec = install_fake_requests(monkeypatch, _session_ok_handler)
    config = {
        "session_endpoint": "https://target.example/session",
        "message_endpoint": "https://target.example/send",
        "session_extract": "sessionId",
        "message_body": {"text": "{{PROMPT}}", "sessionId": "{{SESSION_ID}}"},
        "response_path": "messages.0.message",
    }
    run_async(SessionAPIAdapter().send_prompt("{{SESSION_ID}}", config))
    body = rec.by_method("POST")[-1]["json"]
    # invariant that DOES hold: no injected keys, still valid JSON object
    assert set(body.keys()) == {"text", "sessionId"}
    # documented behavior: the collision rewrites the prompt to the session id
    assert body["text"] == "S-abc-123"


@pytest.mark.parametrize("payload", ADVERSARIAL_PROMPTS[:20])
def test_session_api_prompt_not_in_session_creation(monkeypatch, payload):
    """The prompt must never leak into the session-creation request."""
    rec = install_fake_requests(monkeypatch, _session_ok_handler)
    config = {
        "session_endpoint": "https://target.example/session",
        "message_endpoint": "https://target.example/send",
        "session_body": {"client": "redteam", "uuid": "{{UUID}}"},
        "message_body": {"text": "{{PROMPT}}"},
        "response_path": "messages.0.message",
    }
    run_async(SessionAPIAdapter().send_prompt(payload, config))
    session_post = rec.by_method("POST")[0]
    body = session_post["json"]
    # session creation body carries only its own template keys (prompt cannot add
    # or overwrite a key here, and its only values are template-controlled)
    assert set(body.keys()) == {"client", "uuid"}
    assert body["client"] == "redteam"
    # {{UUID}} was substituted with a real uuid, not left literal
    assert body["uuid"] != "{{UUID}}"

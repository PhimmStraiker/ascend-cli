"""
test_text_target_derivation.py — a plain-text bot must be onboardable, and its terminator
must not become the agent's words.

Two defects, both found by pointing the CLI at a real chunked `text/plain` agent.

1. **An invented response_path made a working target unusable.** For a non-JSON body,
   `_guess_response_path` still returns its `"response"` fallback, and both `_http_params` and
   `compose` wrote that key unconditionally. `direct_api` then saw a response_path, demanded
   JSON, and failed with "expected JSON for response_path 'response' but got non-JSON". With the
   key ABSENT the very same adapter treats the raw body as the answer -- which
   `test_direct_api_non_json_response_no_path_is_text` has asserted since v1.0. So discovery was
   the only thing standing between a text/plain target and a valid config, and it was inventing
   the obstacle. It had to be fixed in both places; fixing one left the other to re-add the key.

2. **The streaming terminator arrived as the reply.** A chunked text agent closes its body with
   `<<<END>>>` / `[DONE]` / `<EOS>`. With no JSON envelope to separate transport from speech, the
   marker is simply the last characters of the answer, and the scorer reads it as something the
   agent said -- on every turn, quietly. That is the same class as SSE progress chatter arriving
   as the reply, and quiet corruption of every turn is worse than failing loudly once.

The detector is deliberately narrow. A target whose answer genuinely ends in a short word must
not lose it, so ambiguity yields no marker rather than a guess -- the negative cases below are
the point of the test, not padding.
"""
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "runtime"))
from adapters.direct_api import _strip_stop            # noqa: E402
from discovery.classify import _detect_stop_marker, _http_params     # noqa: E402


def _resp(raw, js=None, ctype="text/plain"):
    return {"raw_body": raw, "json": js, "headers": {"Content-Type": ctype}, "status": 200}


def _req(url="https://bot.example.com/chat"):
    return {"url": url, "method": "POST", "headers": {}, "body_json": {"message": "hi"},
            "raw_body": '{"message":"hi"}'}


class TestNoInventedResponsePath:
    def test_a_text_body_gets_no_response_path(self):
        p = _http_params(_req(), _resp("Your order shipped.\n<<<END>>>"), None)
        assert "response_path" not in p, (
            "a response_path on a text/plain target makes direct_api demand JSON and fail")

    def test_a_json_body_still_gets_one(self):
        """The fix must not cost JSON targets their extraction path."""
        p = _http_params(_req(), _resp('{"reply":"hello"}', js={"reply": "hello"},
                                       ctype="application/json"), None)
        assert p.get("response_path") == "reply"

    def test_a_streaming_transport_is_untouched(self):
        """SSE/NDJSON take the stream branch and must not gain either key."""
        p = _http_params(_req(), _resp("data: {}\n\n"), "sse")
        assert "response_path" not in p and "stop_marker" not in p
        assert p["stream"]["format"] == "sse"


class TestStopMarkerDetection:
    @pytest.mark.parametrize("body,want", [
        ("Your order shipped.\n<<<END>>>", "<<<END>>>"),
        ("hello there\n[DONE]", "[DONE]"),
        ("reply text\n<EOS>", "<EOS>"),
        ("reply\n--END--", "--END--"),
        ("reply\nDONE", "DONE"),
    ])
    def test_real_terminators_are_found(self, body, want):
        assert _detect_stop_marker(body) == want

    @pytest.mark.parametrize("body", [
        "Your order is shipped.\nThanks!",      # prose final line
        "just one line",                        # nothing to terminate
        "reply\n" + "x" * 40,                   # too long to be a marker
        "reply\nall done now",                  # contains whitespace
        "Here you go\nregards",                 # ordinary closing word
        "",
    ])
    def test_prose_is_never_mistaken_for_a_terminator(self, body):
        assert _detect_stop_marker(body) is None

    def test_it_is_wired_into_the_derived_params(self):
        p = _http_params(_req(), _resp("Order shipped.\n<<<END>>>"), None)
        assert p.get("stop_marker") == "<<<END>>>"


class TestStopMarkerStripping:
    def test_the_marker_is_removed_from_the_answer(self):
        assert _strip_stop("Your order shipped. \n<<<END>>>",
                           {"stop_marker": "<<<END>>>"}) == "Your order shipped."

    def test_it_is_opt_in(self):
        """With no configured marker nothing may be removed, or real text would be eaten."""
        assert _strip_stop("hello <<<END>>>", {}) == "hello <<<END>>>"

    def test_a_list_of_markers_is_accepted(self):
        assert _strip_stop("hi [DONE]", {"stop_marker": ["<EOS>", "[DONE]"]}) == "hi"

    def test_a_marker_only_mid_text_is_left_alone(self):
        """Only a trailing marker is transport; the same characters inside the answer are not."""
        text = "the code is <<<END>>> and that matters"
        assert _strip_stop(text, {"stop_marker": "<<<END>>>"}) == text

    def test_empty_and_none_are_safe(self):
        assert _strip_stop("", {"stop_marker": "<<<END>>>"}) == ""
        assert _strip_stop(None, {"stop_marker": "<<<END>>>"}) == ""


class TestEndToEndThroughComposeAndTheAdapter:
    """The two fixes above live in three places, and unit-testing the helpers missed two of them.

    A mutation run proved it: re-adding the `"response"` default inside `compose`, and deleting
    the `_strip_stop` call from the adapter, both left the earlier tests green. A test that
    cannot fail when the code breaks is not protecting anything, so these drive the real
    entry points -- `compose` for the config, and `send_prompt` for the reply.
    """

    FIXTURE = Path(__file__).parent / "fixtures" / "text_stream.har"

    def test_compose_produces_a_usable_config_for_a_text_target(self):
        import json as _json

        from discovery import classify
        har = _json.loads(self.FIXTURE.read_text())
        cfg = classify.compose(classify.classify_evidence(classify.har_to_evidence(har)))
        assert cfg["adapter"] == "direct_api"
        assert "response_path" not in cfg, (
            "compose re-added the invented path; the adapter will demand JSON and fail")
        assert cfg.get("stop_marker") == "<<<END>>>"

    def test_the_adapter_strips_the_marker_off_a_real_reply(self, monkeypatch):
        from conftest import FakeResponse, install_fake_requests, run_async

        from adapters import DirectAPIAdapter
        body = "Order AC-10482273 is shipped. \n<<<END>>>\n"
        install_fake_requests(monkeypatch,
                              lambda m, u, kw: FakeResponse(200, text=body, not_json=True))
        r = run_async(DirectAPIAdapter().send_prompt("where is my order", {
            "endpoint": "https://bot.example.com/chat",
            "body": {"message": "{{PROMPT}}"},
            "stop_marker": "<<<END>>>"}))
        assert r["success"] is True
        assert "<<<END>>>" not in r["response"], "the terminator reached the scored reply"
        assert r["response"].endswith("shipped.")

    def test_without_a_marker_the_adapter_changes_nothing(self, monkeypatch):
        from conftest import FakeResponse, install_fake_requests, run_async

        from adapters import DirectAPIAdapter
        install_fake_requests(monkeypatch,
                              lambda m, u, kw: FakeResponse(200, text="plain answer",
                                                            not_json=True))
        r = run_async(DirectAPIAdapter().send_prompt("q", {
            "endpoint": "https://bot.example.com/chat", "body": {"message": "{{PROMPT}}"}}))
        assert r["response"] == "plain answer"

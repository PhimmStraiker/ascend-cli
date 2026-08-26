"""Regression tests for capture verification + response-path ground truth.

These lock in the fixes for the worst discovery failure mode: emitting a
confident config from a capture that never actually reached the chat widget.
"""
import sys, os, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "runtime"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from runtime.discovery import classify as C

PROMPT = "Hello, what can you help me with?"


def _pair(url, req_body, resp_body, ctype="application/json"):
    return {"request": {"method": "POST", "url": url,
                        "headers": [{"name": "Content-Type", "value": "application/json"}],
                        "raw_body": req_body},
            "response": {"status": 200,
                         "headers": [{"name": "Content-Type", "value": ctype}],
                         "raw_body": resp_body, "content_type": ctype}}


def test_response_path_uses_captured_reply_as_ground_truth():
    """Was picking top-level 'status' -> 'ok' instead of the nested real answer."""
    answer = "You asked: hi. I can help with orders and returns."
    ev = {"pairs": [_pair("https://b.example/api/agent/chat",
                          json.dumps({"query": PROMPT, "channel": "web"}),
                          json.dumps({"status": "ok", "data": {"answer": answer}}))],
          "ws_messages": [], "prompt_sent": PROMPT,
          "reply_text": f"Assistant: {answer}"}
    out = C.classify_evidence(ev)
    assert out["config"]["response_path"] == "data.answer"
    assert out["config"]["body"] == {"query": "{{PROMPT}}", "channel": "web"}


def test_response_path_falls_back_to_longest_string_anywhere_not_top_level():
    long_answer = "This is the actual assistant answer, which is long."
    ev = {"pairs": [_pair("https://b.example/chat", json.dumps({"q": PROMPT}),
                          json.dumps({"status": "ok", "meta": {"reply": long_answer}}))],
          "ws_messages": [], "prompt_sent": PROMPT}
    out = C.classify_evidence(ev)
    assert out["config"]["response_path"] == "meta.reply"


def test_paths_to_strings_walks_nested_structures():
    got = dict(C._paths_to_strings({"a": {"b": ["x", {"c": "y"}]}, "d": "z"}))
    assert got["a.b.0"] == "x" and got["a.b.1.c"] == "y" and got["d"] == "z"


def test_unverified_capture_yields_no_chat_pair():
    """No prompt in traffic => nothing to anchor on; must not invent a chat call."""
    ev = {"pairs": [_pair("https://cdn.example/bootstrap", "{}",
                          json.dumps({"config": "x" * 500}))],
          "ws_messages": [], "prompt_sent": None}
    out = C.classify_evidence(ev)
    # transport may guess, but confidence must stay low / layer unresolved
    assert out["overall_confidence"] < 0.8

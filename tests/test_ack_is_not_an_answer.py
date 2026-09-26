"""An acknowledgement is not an answer.

Two derivation defects let a target whose reply to the message is only an acknowledgement — the
send half of an ack-then-poll contract, captured without the later poll — be wired as though the
acknowledgement were the bot's answer:

1. The answer-path fallback took the LONGEST string anywhere in the body. In `{"id": "<uuid>",
   "status": "queued"}` that is the id, and a fresh id comes back per request, so two different
   test questions got two different "answers" — which is exactly what the constant-reply guard
   accepts as a live bot. A false pass the guard cannot see. An identifier is now never an answer.

2. Any JSON body was classified as a plain JSON endpoint at confidence 0.85. A body that carries an
   id and no answer text is now reported below the resolve threshold, so the layer shows up
   unresolved instead of wired with false certainty.

Synthetic bodies — no customer data.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from runtime.discovery import classify

UUID = "3f2b8c1e-9a4d-4e6f-b7c2-1d5e8a9f0b3c"


def _evidence(resp_json_text, prompt="what is 2+2?"):
    """A one-exchange capture: POST the prompt, get `resp_json_text` back."""
    entry = {
        "request": {"method": "POST", "url": "https://bot.example/api/chat",
                    "headers": [{"name": "content-type", "value": "application/json"}],
                    "postData": {"text": '{"message": "%s"}' % prompt}},
        "response": {"status": 200,
                     "headers": [{"name": "content-type", "value": "application/json"}],
                     "content": {"text": resp_json_text, "mimeType": "application/json"}},
    }
    return classify.har_to_evidence({"log": {"entries": [entry]}}, prompt_sent=prompt)


# ---- 1. the answer path never lands on an identifier -------------------------------------------

def test_the_answer_path_never_lands_on_an_id():
    path = classify._guess_response_path_raw({"id": UUID, "status": "queued"})
    assert path != "id", path
    assert path == "status", path


def test_an_id_does_not_outrank_a_short_nested_answer():
    # Before: the 36-char uuid was the longest string, so the path was "id" and the answer was lost.
    path = classify._guess_response_path_raw({"id": UUID, "data": {"answer": "Paris"}})
    assert path == "data.answer", path


def test_a_long_token_value_is_not_an_answer_whatever_its_key():
    token = "a3f09c" * 8                                   # 48 hex chars, no key hint
    path = classify._guess_response_path_raw({"ref": token, "state": "Sent"})
    assert path == "state", path


def test_real_answers_are_picked_exactly_as_before():
    assert classify._guess_response_path_raw({"response": "Paris is the capital of France."}) == "response"
    assert classify._guess_response_path_raw(
        {"status": "ok", "data": {"reply": "The weather is sunny today."}}) == "data.reply"
    assert classify._guess_response_path_raw(
        {"choices": [{"message": {"content": "Hi there"}}]}) == "choices.0.message.content"


# ---- 2. a bare acknowledgement is reported unresolved, not wired at 0.85 -----------------------

def test_a_bare_acknowledgement_is_not_wired_with_confidence():
    res = classify.classify_evidence(_evidence('{"id": "%s", "status": "queued"}' % UUID))
    t = res["layers"]["transport"]
    assert t["confidence"] < classify.LOW_CONF, t
    assert "acknowledgement" in t["evidence"], t
    assert "transport" in res["unresolved"], res["unresolved"]


def test_a_real_answer_that_also_carries_an_id_keeps_its_confidence():
    body = '{"id": "%s", "reply": "Hello there, how can I help you today?"}' % UUID
    t = classify.classify_evidence(_evidence(body))["layers"]["transport"]
    assert t["value"] == "rest_json" and t["confidence"] == 0.85, t


def test_a_terse_answer_under_a_known_key_is_not_an_acknowledgement():
    assert classify._is_bare_ack({"id": UUID, "answer": "4"}) is False
    assert classify._is_bare_ack({"id": UUID, "status": "queued"}) is True
    assert classify._is_bare_ack({"status": "queued"}) is False            # no id: not this shape
    assert classify._is_bare_ack("plain text") is False


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok ", name)

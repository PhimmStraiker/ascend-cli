"""Regression tests for discovery accuracy bugs found during live bot hunting.

Both bugs produced CONFIDENTLY WRONG configs, which is the worst failure mode for
an auto-discovery tool — worse than admitting low confidence.
"""
import sys, os, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "runtime"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pytest
from runtime.discovery import classify as C


def _pair(url, req_body, resp_body, ctype="application/json", method="POST", status=200):
    return {
        "request": {"method": method, "url": url,
                    "headers": [{"name": "Content-Type", "value": "application/json"}],
                    "raw_body": req_body},
        "response": {"status": status,
                     "headers": [{"name": "Content-Type", "value": ctype}],
                     "raw_body": resp_body, "content_type": ctype},
    }


PROMPT = "Hello, what can you help me with?"


def test_analytics_websocket_is_not_mistaken_for_the_chat_transport():
    """BUG: any WS frames -> transport=websocket, even an analytics vendor's socket."""
    ev = {
        "pairs": [_pair("https://bot.example.com/api/chat",
                        json.dumps({"prompt": PROMPT}),
                        json.dumps({"reply": "Sure, I can help."}))],
        # a marketing/personalization socket that never carried our prompt
        "ws_messages": [{"url": "wss://analytics.vendor.com/websock/site",
                         "sent": ['{"event":"pageview"}'], "received": ['{"ok":true}']}],
        "prompt_sent": PROMPT,
    }
    out = C.classify_evidence(ev)
    assert out["layers"]["transport"]["value"] == "rest_json"
    assert "analytics.vendor.com" not in json.dumps(out["config"])
    assert out["config"].get("endpoint") == "https://bot.example.com/api/chat"


def test_websocket_that_did_carry_the_prompt_is_selected():
    ev = {
        "pairs": [_pair("https://site.example.com/api/telemetry", "{}", "{}")],
        "ws_messages": [{"url": "wss://bot.example.com/chat",
                         "sent": [json.dumps({"type": "message", "text": PROMPT})],
                         "received": [json.dumps({"type": "token", "text": "hi"})]}],
        "prompt_sent": PROMPT,
    }
    out = C.classify_evidence(ev)
    assert out["layers"]["transport"]["value"] == "websocket"


def test_known_prompt_beats_size_heuristic_for_chat_pair():
    """BUG: the biggest response won, even when a smaller request held our prompt."""
    big_noise = json.dumps({"junk": "x" * 5000})
    ev = {
        "pairs": [
            _pair("https://cdn.example.com/api/config", "{}", big_noise),   # biggest
            _pair("https://bot.example.com/api/chat",
                  json.dumps({"message": PROMPT}),
                  json.dumps({"reply": "short but correct"})),
        ],
        "ws_messages": [],
        "prompt_sent": PROMPT,
    }
    out = C.classify_evidence(ev)
    assert out["chat_pair_index"] == 1
    assert out["config"].get("endpoint") == "https://bot.example.com/api/chat"


def test_sentinel_framing_is_detected():
    body = ('BOT_CHAT_EVENT_BEGIN{"conversationID":"c1"}BOT_CHAT_EVENT_END\n\n'
            'BOT_CHAT_EVENT_BEGIN{"type":"state","state":{"events":['
            '{"message":{"author":"AGENT","text":"hello there"}}]}}BOT_CHAT_EVENT_END')
    ev = {"pairs": [_pair("https://bot.example.com/-/api/chat",
                          json.dumps({"userMessageText": PROMPT}), body,
                          ctype="text/plain")],
          "ws_messages": [], "prompt_sent": PROMPT}
    out = C.classify_evidence(ev)
    t = out["layers"]["transport"]
    assert t["value"] == "sentinel_stream" and t["confidence"] >= 0.8
    cfg = out["config"]
    assert cfg["adapter"] == "sentinel_stream"
    assert cfg["begin_marker"] == "BOT_CHAT_EVENT_BEGIN"
    assert cfg["end_marker"] == "BOT_CHAT_EVENT_END"
    assert cfg["url"] == "https://bot.example.com/-/api/chat"


def test_custom_sentinel_markers_are_discovered_generically():
    body = 'ACME_FRAME_BEGIN{"message":{"author":"bot","text":"yo"}}ACME_FRAME_END'
    ev = {"pairs": [_pair("https://x.example.com/chat",
                          json.dumps({"q": PROMPT}), body, ctype="text/plain")],
          "ws_messages": [], "prompt_sent": PROMPT}
    out = C.classify_evidence(ev)
    assert out["layers"]["transport"]["value"] == "sentinel_stream"
    assert out["config"]["begin_marker"] == "ACME_FRAME_BEGIN"


def test_handshake_only_websocket_is_ignored():
    """A socket that opened but carried no frames is not the chat channel."""
    ev = {
        "pairs": [_pair("https://bot.example.com/api/chat",
                        json.dumps({"prompt": PROMPT}), json.dumps({"reply": "ok"}))],
        "ws_messages": [{"url": "wss://push.example.com/live", "sent": [], "received": []}],
        "prompt_sent": PROMPT,
    }
    out = C.classify_evidence(ev)
    assert out["layers"]["transport"]["value"] == "rest_json"

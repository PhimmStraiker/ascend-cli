"""
`ascend adaptor har` — the first thing an SE runs, on a file full of live credentials.

Ported from the SE toolkit's HAR-reading suite. Two things have to hold. It must FIND the
session chain, because that is the whole reason to read a HAR: a value a response produced and a
later request carried is a login, a token mint or a conversation id, and each one is a step the
adaptor performs in order. And it must never print a usable secret — in the text OR in the
`--json` structure — because a HAR records a real session and this output is meant to be pasted
into a chat with an agent.
"""
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "runtime"))
from discovery import har_chains as HC  # noqa: E402

TOKEN = "eyJhbGciOiJIUzI1NiJ9.dGhpcy1pcy1hLWxvbmctbGl2ZS10b2tlbg.signature"
COOKIE = "abc123def456ghi789jkl"
CONV = "conv-8f41c2ab-77de-4a19-9e55-1234567890ab"


def _h(**kw):
    return [{"name": k.replace("_", "-"), "value": v} for k, v in kw.items()]


def har_with_login(tmp_path):
    entries = [
        {"request": {"method": "GET", "url": "https://t.example/login", "headers": []},
         "response": {"status": 200, "headers": _h(set_cookie=f"XSRF-TOKEN={COOKIE}; Path=/"),
                      "content": {"text": "<html>"}}},
        {"request": {"method": "POST", "url": "https://t.example/auth/token",
                     "headers": _h(cookie=f"XSRF-TOKEN={COOKIE}")},
         "response": {"status": 200, "headers": _h(content_type="application/json"),
                      "content": {"text": json.dumps({"accessToken": TOKEN})}}},
        {"request": {"method": "POST", "url": "https://t.example/conversation",
                     "headers": _h(authorization=f"Bearer {TOKEN}")},
         "response": {"status": 201, "headers": _h(content_type="application/json"),
                      "content": {"text": json.dumps({"conversationId": CONV})}}},
        {"request": {"method": "GET", "url": "https://cdn.example/logo.png", "headers": []},
         "response": {"status": 200, "headers": _h(content_type="image/png"),
                      "content": {"text": ""}}},
    ]
    p = tmp_path / "login.har"
    p.write_text(json.dumps({"log": {"entries": entries}}))
    return p


def har_single_call(tmp_path):
    entries = [
        {"request": {"method": "POST", "url": "https://t.example/chat", "headers": [],
                     "postData": {"text": json.dumps({"prompt": "hi"})}},
         "response": {"status": 200, "headers": _h(content_type="application/json"),
                      "content": {"text": json.dumps({"reply": "hello"})}}},
    ]
    p = tmp_path / "simple.har"
    p.write_text(json.dumps({"log": {"entries": entries}}))
    return p


def run(path, **kw):
    rep = HC.read_chains(str(path), ignore=kw.get("ignore", HC.DEFAULT_IGNORE),
                         bodies=kw.get("bodies", False))
    return rep, HC.render(rep)


class TestItFindsTheChain:
    def test_each_hop_is_reported(self, tmp_path):
        rep, out = run(har_with_login(tmp_path))
        assert "#1 cookie XSRF-TOKEN  ->  #2" in out
        assert "#2 accessToken  ->  #3" in out
        assert rep["chains"] == [{"from": 1, "label": "cookie XSRF-TOKEN", "to": 2},
                                 {"from": 2, "label": "accessToken", "to": 3}]

    def test_the_header_says_where_its_value_came_from(self, tmp_path):
        """Reading it in the sequence is what turns a list of calls into an order."""
        rep, out = run(har_with_login(tmp_path))
        assert "<- from #2 accessToken" in out
        assert rep["sequence"][2]["auth"][0]["from"] == {"n": 2, "label": "accessToken"}

    def test_static_assets_are_dropped(self, tmp_path):
        rep, out = run(har_with_login(tmp_path))
        assert "cdn.example" not in out
        assert "3 request(s) kept of 4" in out
        assert (rep["kept"], rep["total"]) == (3, 4)

    def test_hosts_are_counted_busiest_first(self, tmp_path):
        rep, out = run(har_with_login(tmp_path))
        assert rep["hosts"] == [("t.example", 3)]
        assert "_adaptor_domains" in out


class TestItDoesNotInventSteps:
    def test_a_single_call_reports_no_chains(self, tmp_path):
        """The answer "this target is one request" has to be sayable, or every adaptor grows a
        login it does not need."""
        rep, out = run(har_single_call(tmp_path))
        assert rep["chains"] == []
        assert "none found" in out and "ONE request" in out


class TestItNeverPrintsAUsableSecret:
    """A HAR is a recording of a real session and this output gets pasted into a chat."""

    def test_the_bearer_token_is_redacted_in_text_and_data(self, tmp_path):
        rep, out = run(har_with_login(tmp_path))
        assert TOKEN not in out, "the live token was printed"
        assert TOKEN not in json.dumps(rep), "the live token is in the --json structure"
        # Still correlatable: the ellipsis and byte count let a reader match the same value
        # across requests without being able to use it.
        assert "…" in out and "b)" in out

    def test_the_session_cookie_is_redacted(self, tmp_path):
        rep, out = run(har_with_login(tmp_path))
        assert COOKIE not in out and COOKIE not in json.dumps(rep)

    def test_the_conversation_id_is_not_leaked_either(self, tmp_path):
        rep, out = run(har_with_login(tmp_path))
        assert CONV not in out and CONV not in json.dumps(rep)

    def test_bodies_mode_shows_shapes_not_values(self, tmp_path):
        rep, out = run(har_single_call(tmp_path), bodies=True)
        assert '"prompt": "str"' in out
        assert '"reply": "str"' in out
        assert "hello" not in out and "hello" not in json.dumps(rep)

    @pytest.mark.parametrize("value,expect", [
        ("abcdefghijklmnopqrstuvwxyz", "abcd…wxyz (26b)"),
        ("short", "<5b>"),
        ("", "<0b>"),
        (None, "<0b>"),
    ])
    def test_redact(self, value, expect):
        assert HC.redact(value) == expect


class TestItFailsUsefully:
    def test_a_file_that_is_not_a_har(self, tmp_path):
        p = tmp_path / "x.har"
        p.write_text("not json")
        with pytest.raises(HC.HarError, match="HAR JSON"):
            run(p)

    def test_a_har_with_no_entries(self, tmp_path):
        p = tmp_path / "empty.har"
        p.write_text(json.dumps({"log": {"entries": []}}))
        with pytest.raises(HC.HarError, match="no entries"):
            run(p)

    def test_a_missing_file(self, tmp_path):
        with pytest.raises(HC.HarError):
            run(tmp_path / "nope.har")

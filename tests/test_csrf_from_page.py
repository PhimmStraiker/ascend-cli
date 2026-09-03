"""
test_csrf_from_page.py — a CSRF token lives in the PAGE, and the page must survive capture.

Two independent defects stacked into one failure, both found by pointing the browser capture at
a real CSRF-gated chat page.

1. **The document was thrown away.** `_worth_recording` keeps every POST but keeps a GET only if
   its URL matches a hardcoded "chatty" word list. The page being captured is served from `/`,
   which matches none of them — so the one response that bootstraps everything (the CSRF token
   in a `<meta>` tag, the session cookie, any inline config) was discarded before classification
   saw it. The function's own docstring makes exactly this argument for POSTs — "a discovery tool
   that can only see endpoints it already expects is not discovering anything" — and left GETs
   subject to it.

2. **Origin-scanning only read JSON.** `_collect_prior_values` walks `response["json"]` string
   leaves, and an HTML page is not JSON, so even a captured page could not yield the token. Auth
   then classified as "origin not in capture" and composed `bootstrap_url: ""`, which the auth
   layer refuses outright with "csrf auth requires 'bootstrap_url'". The auth layer has always
   been able to regex a token out of an HTML bootstrap body — only the finding was missing.

The generated regex is keyed on the attribute NAME, never the token value: the value rotates
every session, so anchoring on it would produce a config that worked exactly once and then
failed as a mysterious auth error. It also uses a lookahead rather than a fixed attribute order,
because the first version emitted `name=... content=...` after matching a tag written
`content=... name=...` — a regex that could not re-extract the token it had just found.
"""
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "runtime"))
from discovery import classify                      # noqa: E402
from discovery.capture import _worth_recording      # noqa: E402

TOKEN = "abc123def456abc123def456"


def _pair(body, url="https://bot.example.com/"):
    return {"request": {"url": url, "method": "GET", "headers": {}},
            "response": {"raw_body": body, "json": None}}


class TestTheDocumentIsAlwaysKept:
    def test_the_page_survives_even_with_a_boring_url(self):
        """`/` contains no 'chat'-like word, and it is the most important response there is."""
        assert _worth_recording("https://bot.example.com/", "GET", "document") is True
        assert _worth_recording("https://shop.example.com/support", "GET", "document") is True

    @pytest.mark.parametrize("url,method", [
        ("https://bot.example.com/legal/terms", "GET"),
        ("https://bot.example.com/api/chat", "POST"),
        ("https://bot.example.com/app.css", "GET"),
        ("https://bot.example.com/telemetry/beacon", "GET"),
    ])
    def test_the_new_parameter_defaults_to_the_old_behaviour(self, url, method):
        """`resource_type` was added to an existing signature; every old call must be unchanged.

        This is the regression guard that matters. Asserting a "boring GET is still dropped" is
        not constructible -- INTERESTING matches almost any path -- so the honest check is that
        omitting the new argument gives exactly what a non-document call gives.
        """
        assert _worth_recording(url, method) == _worth_recording(url, method, "xhr")

    def test_a_chatty_get_is_kept_as_before(self):
        assert _worth_recording("https://bot.example.com/api/chat/history", "GET", "xhr") is True

    def test_static_assets_are_still_dropped_even_as_documents(self):
        """A .css served as a document is still an asset; the STATIC guard runs first."""
        assert _worth_recording("https://bot.example.com/app.css", "GET", "document") is False

    def test_posts_are_unaffected(self):
        assert _worth_recording("https://bot.example.com/anything", "POST", "xhr") is True


class TestTokenIsFoundInHtml:
    @pytest.mark.parametrize("body,where", [
        (f'<html><meta name="csrf-token" content="{TOKEN}"></html>', "meta tag"),
        (f'<html><meta content="{TOKEN}" name="csrf-token"></html>', "meta tag"),
        (f'<form><input type="hidden" name="_csrf" value="{TOKEN}"></form>', "hidden input"),
        (f'<form><input value="{TOKEN}" name="_csrf"></form>', "hidden input"),
        (f'<input name="authenticity_token" value="{TOKEN}">', "hidden input"),
        (f'<meta name="xsrf-token" content="{TOKEN}">', "meta tag"),
        (f'<script>window.cfg={{"csrfToken":"{TOKEN}"}}</script>', "inline script"),
    ])
    def test_every_common_placement(self, body, where):
        got = classify._html_token_origin([_pair(body)], 1, TOKEN)
        assert got is not None, f"token not found in {where}"
        url, rx, kind = got
        assert kind == where

    @pytest.mark.parametrize("body,where", [
        (f'<html><meta name="csrf-token" content="{TOKEN}"></html>', "meta tag"),
        (f'<html><meta content="{TOKEN}" name="csrf-token"></html>', "meta tag"),
        (f'<form><input value="{TOKEN}" name="_csrf"></form>', "hidden input"),
        (f'<script>window.cfg={{"csrfToken":"{TOKEN}"}}</script>', "inline script"),
    ])
    def test_the_emitted_regex_re_extracts_the_token(self, body, where):
        """The whole point: the regex must work on a FRESH bootstrap, in either attribute order."""
        _url, rx, _kind = classify._html_token_origin([_pair(body)], 1, TOKEN)
        m = re.search(rx, body, re.I)
        assert m and m.group(1) == TOKEN, f"regex {rx!r} could not re-extract from {where}"

    def test_the_regex_is_keyed_on_the_name_not_the_value(self):
        """A rotating token means a value-anchored regex works exactly once."""
        body = f'<meta name="csrf-token" content="{TOKEN}">'
        _url, rx, _ = classify._html_token_origin([_pair(body)], 1, TOKEN)
        assert TOKEN not in rx, "the token value was baked into the extraction regex"
        rotated = '<meta name="csrf-token" content="a-completely-different-token">'
        m = re.search(rx, rotated, re.I)
        assert m and m.group(1) == "a-completely-different-token"

    @pytest.mark.parametrize("body", [
        "<html><body>nothing here</body></html>",
        '<meta name="csrf-token" content="SOME-OTHER-VALUE">',
        "",
    ])
    def test_no_false_positive(self, body):
        assert classify._html_token_origin([_pair(body)], 1, TOKEN) is None

    def test_a_short_needle_is_ignored(self):
        """A 3-character header value is not a token; matching it would find noise."""
        assert classify._html_token_origin([_pair('<meta name="csrf-token" content="ab">')],
                                           1, "ab") is None


class TestIncompleteCsrfSaysWhatIsMissing:
    def test_an_unfindable_origin_carries_an_explanation(self):
        """Emitting bootstrap_url:"" guarantees a hard failure that reads like a capture problem.

        The operator needs to know the capture is missing the page load, not that the tool broke.
        """
        har = {"log": {"version": "1.2", "entries": [{
            "startedDateTime": "2026-09-03T00:00:00.000Z", "time": 5,
            "request": {"method": "POST", "url": "https://bot.example.com/api/chat",
                        "httpVersion": "HTTP/1.1", "queryString": [], "cookies": [],
                        "headers": [{"name": "X-CSRF-Token", "value": TOKEN},
                                    {"name": "Content-Type", "value": "application/json"}],
                        "postData": {"mimeType": "application/json",
                                     "text": '{"message":"hi"}'},
                        "headersSize": -1, "bodySize": 17},
            "response": {"status": 200, "statusText": "OK", "httpVersion": "HTTP/1.1",
                         "headers": [{"name": "Content-Type", "value": "application/json"}],
                         "cookies": [], "redirectURL": "", "headersSize": -1, "bodySize": 15,
                         "content": {"size": 15, "mimeType": "application/json",
                                     "text": '{"reply":"hello there"}'}},
            "cache": {}, "timings": {"send": 0, "wait": 0, "receive": 0}}]}}
        ev = classify.har_to_evidence(har)
        auth = classify.classify_auth(ev, 0)
        assert auth["value"] == "csrf"
        assert auth["params"]["bootstrap_url"] == ""
        assert "_incomplete" in auth["params"]
        assert "bootstrap_url" in auth["params"]["_incomplete"]


class TestClassifyAuthActuallyUsesTheHtmlOrigin:
    """Driving `classify_auth`, not the helper.

    A mutation run showed why this is needed: replacing the `_html_token_origin(...)` call inside
    `classify_auth` with `None` left every test above green, because they all call the helper
    directly. A test that cannot fail when the wiring breaks is not protecting the wiring.
    """

    def _har(self, page_html):
        def entry(method, url, headers, resp_body, mime, status=200, post=None):
            e = {"startedDateTime": "2026-09-03T00:00:00.000Z", "time": 5,
                 "request": {"method": method, "url": url, "httpVersion": "HTTP/1.1",
                             "queryString": [], "cookies": [], "headersSize": -1, "bodySize": 0,
                             "headers": [{"name": k, "value": v} for k, v in headers.items()]},
                 "response": {"status": status, "statusText": "OK", "httpVersion": "HTTP/1.1",
                              "headers": [{"name": "Content-Type", "value": mime}],
                              "cookies": [], "redirectURL": "", "headersSize": -1,
                              "bodySize": len(resp_body),
                              "content": {"size": len(resp_body), "mimeType": mime,
                                          "text": resp_body}},
                 "cache": {}, "timings": {"send": 0, "wait": 0, "receive": 0}}
            if post:
                e["request"]["postData"] = {"mimeType": "application/json", "text": post}
                e["request"]["bodySize"] = len(post)
            return e
        return {"log": {"version": "1.2", "entries": [
            # the page: this is the response that was being thrown away
            entry("GET", "https://bot.example.com/", {}, page_html, "text/html"),
            # the chat call, echoing the token back in a header
            entry("POST", "https://bot.example.com/api/chat",
                  {"X-CSRF-Token": TOKEN, "Content-Type": "application/json"},
                  '{"reply":"Your order has shipped."}', "application/json",
                  post='{"message":"where is my order?"}'),
        ]}}

    def test_the_bootstrap_url_and_regex_come_from_the_page(self):
        html = f'<html><head><meta name="csrf-token" content="{TOKEN}"></head></html>'
        ev = classify.har_to_evidence(self._har(html))
        auth = classify.classify_auth(ev, 1)
        assert auth["value"] == "csrf"
        assert auth["params"]["bootstrap_url"] == "https://bot.example.com/", \
            "the page that issues the token was not identified"
        rx = auth["params"]["extract"].get("regex")
        assert rx, "no extraction regex was emitted"
        m = re.search(rx, html, re.I)
        assert m and m.group(1) == TOKEN
        # Compared case-insensitively on purpose. `_headers_to_dict` lowercases at normalization
        # and the original spelling is discarded, so `_orig_header_name` can only return a
        # canonicalized guess -- "X-CSRF-Token" comes back as "X-Csrf-Token". Header names are
        # case-insensitive per RFC 7230 so this is harmless here, but it is NOT harmless for
        # every header: the same path turns "SOAPAction" into "Soapaction", which strict SOAP
        # stacks and some gateways do reject. Tracked separately; asserting the exact casing here
        # would just pin the wrong behaviour.
        assert auth["params"]["into_header"].lower() == "x-csrf-token"
        assert "_incomplete" not in auth["params"], \
            "a complete csrf block must not be flagged incomplete"

    def test_a_hidden_input_page_also_resolves(self):
        html = f'<html><form><input type="hidden" name="_csrf" value="{TOKEN}"></form></html>'
        ev = classify.har_to_evidence(self._har(html))
        auth = classify.classify_auth(ev, 1)
        assert auth["params"]["bootstrap_url"] == "https://bot.example.com/"
        assert re.search(auth["params"]["extract"]["regex"], html, re.I).group(1) == TOKEN

    def test_a_page_without_the_token_is_reported_as_incomplete(self):
        """Still a csrf target, but the capture cannot show where the token comes from."""
        ev = classify.har_to_evidence(self._har("<html><body>no token here</body></html>"))
        auth = classify.classify_auth(ev, 1)
        assert auth["value"] == "csrf"
        assert auth["params"]["bootstrap_url"] == ""
        assert "_incomplete" in auth["params"]

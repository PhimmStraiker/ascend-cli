"""
test_secret_headers.py — a credential under a name we did not anticipate must not reach disk.

`classify.py`'s docstring promises that secrets "carry an `env:` `value_ref` placeholder instead,
and record only the header". It did not hold. `_SECRET_HEADERS` is a fixed list of nine names,
and that one list drove BOTH questions: "is this auth?" and "is this safe to bake into a config?"

So a custom-named credential — `X-Tenant-Key`, `X-Subscription-Key`, `X-Nonce`,
`X-Session-Token` — was neither recognised as auth NOR dropped, and was written into the config
on disk in cleartext beside `auth: none`. Worse, it *validated green* precisely because the
credential had been copied, so nothing signalled a problem. Same class as the two 1.1.1 security
fixes (bridge keys in `app list`, credentials in a URL query string): the leak came from the
command least likely to be suspected of holding a secret.

Recognition has to be open-ended, because the entire point is that the header name is one nobody
listed in advance. Two rules, in order:

  1. **Name.** A broad vocabulary (key/token/secret/signature/hmac/nonce/session/credential/...),
     because a name is deliberate where a value is circumstantial.
  2. **Entropy, scoped to `x-*`.** The backstop for a name that cannot be anticipated. Scoped so
     an ordinary long-but-public header is not stripped from a config that needs it — a
     User-Agent, a `traceparent`, a request id are all long and opaque and none are credentials.

The negative cases below are the substance of this test, not filler: over-dropping is its own
outage, because the config then 401s with the required header missing.
"""
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "runtime"))
from discovery import classify as C     # noqa: E402

SECRET = "tk_live_42abcdef0123456789"


def _har(headers):
    return {"log": {"version": "1.2", "entries": [{
        "startedDateTime": "2026-09-03T00:00:00.000Z", "time": 5,
        "request": {"method": "POST", "url": "https://bot.example.com/api/chat",
                    "httpVersion": "HTTP/1.1", "queryString": [], "cookies": [],
                    "headersSize": -1, "bodySize": 30,
                    "headers": [{"name": k, "value": v} for k, v in headers.items()],
                    "postData": {"mimeType": "application/json",
                                 "text": '{"message":"where is my order?"}'}},
        "response": {"status": 200, "statusText": "OK", "httpVersion": "HTTP/1.1",
                     "headers": [{"name": "Content-Type", "value": "application/json"}],
                     "cookies": [], "redirectURL": "", "headersSize": -1, "bodySize": 24,
                     "content": {"size": 24, "mimeType": "application/json",
                                 "text": '{"reply":"It shipped."}'}},
        "cache": {}, "timings": {"send": 0, "wait": 0, "receive": 0}}]}}


class TestCredentialsNeverReachTheConfig:
    @pytest.mark.parametrize("name", [
        "X-Tenant-Key", "X-Subscription-Key", "X-Nonce", "X-Session-Token",
        "X-Signature", "X-Client-Secret", "X-Access-Key", "X-Hmac",
        "X-Custom-Blob",                     # unanticipated name, caught by entropy
    ])
    def test_the_value_is_not_written_to_disk(self, name):
        cfg = C.compose(C.classify_evidence(C.har_to_evidence(
            _har({name: SECRET, "Content-Type": "application/json"}))))
        assert SECRET not in json.dumps(cfg), \
            f"{name} was baked into the config in cleartext"

    def test_the_name_is_recorded_so_it_can_be_re_supplied(self):
        """Dropping a required header silently just moves the confusion to an unexplained 401."""
        cfg = C.compose(C.classify_evidence(C.har_to_evidence(
            _har({"X-Tenant-Key": SECRET, "Content-Type": "application/json"}))))
        assert cfg.get("_withheld_headers") == ["X-Tenant-Key"]

    def test_only_names_are_recorded_never_values(self):
        cfg = C.compose(C.classify_evidence(C.har_to_evidence(_har({"X-Tenant-Key": SECRET}))))
        assert SECRET not in json.dumps(cfg.get("_withheld_headers"))


class TestOrdinaryHeadersSurvive:
    """Over-dropping is its own outage: the config 401s with the header the target needed."""

    @pytest.mark.parametrize("name,value", [
        ("X-Channel", "web"),
        ("User-Agent", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"),
        ("Accept-Language", "en-US,en;q=0.9"),
        ("X-Requested-With", "XMLHttpRequest"),
        ("X-Request-Id", "0b3f2a1c-1111-2222-3333-444455556666"),
        ("X-Trace-Id", "abcdef0123456789abcdef0123456789"),
        ("Traceparent", "00-abcdef0123456789abcdef0123456789-0123456789abcdef-01"),
    ])
    def test_a_non_credential_header_is_kept(self, name, value):
        cfg = C.compose(C.classify_evidence(C.har_to_evidence(
            _har({name: value, "Content-Type": "application/json"}))))
        kept = {k.lower(): v for k, v in (cfg.get("headers") or {}).items()}
        assert kept.get(name.lower()) == value, f"{name} was dropped but is not a credential"

    def test_a_mixed_capture_keeps_the_safe_and_drops_the_secret(self):
        cfg = C.compose(C.classify_evidence(C.har_to_evidence(_har({
            "X-Tenant-Key": SECRET,
            "X-Channel": "web",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 AppleWebKit/537.36",
        }))))
        kept = cfg.get("headers") or {}
        assert kept.get("X-Channel") == "web"
        assert "Content-Type" in kept and "User-Agent" in kept
        assert SECRET not in json.dumps(cfg)
        assert cfg["_withheld_headers"] == ["X-Tenant-Key"]


class TestThePredicateDirectly:
    @pytest.mark.parametrize("name,value", [
        ("x-tenant-key", SECRET),
        ("x-subscription-key", "8f14e45fceea167a5a36dedd4bea2543"),
        ("x-nonce", "a89e98ff2c1d4e6b"),
        ("authorization", "Bearer abc"),
        ("x-api-key", "k"),                       # name alone is enough; length is irrelevant
        ("x-custom-blob", "AbCdEf0123456789_-=abcdef"),
    ])
    def test_secret(self, name, value):
        assert C._looks_secret_header(name, value) is True

    @pytest.mark.parametrize("name,value", [
        ("user-agent", "Mozilla/5.0 (Macintosh) AppleWebKit/537.36 Chrome/120"),
        ("content-type", "application/json"),
        ("origin", "https://bot.example.com"),
        ("referer", "https://bot.example.com/support/chat?tab=1"),
        ("x-requested-with", "XMLHttpRequest"),
        ("x-request-id", "0b3f2a1c-1111-2222-3333-444455556666"),
        ("x-channel", "web"),
        ("accept", "*/*"),
    ])
    def test_not_secret(self, name, value):
        assert C._looks_secret_header(name, value) is False

    def test_the_reporting_helper_returns_names_only(self):
        held = C.dropped_secret_headers({"x-tenant-key": SECRET, "x-channel": "web"})
        assert held == ["X-Tenant-Key"]
        assert SECRET not in " ".join(held)

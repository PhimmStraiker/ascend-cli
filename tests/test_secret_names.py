"""
test_secret_names.py — ONE rule says which names carry a credential, and every list that used to
decide it on its own now asks that rule.

MEASURED 2026-10-09 on the lab target. Its access code arrives as `x-lab-code` (and as a
`Cookie`). `target add --header 'x-lab-code: <literal>'` wrote both literals into the config and
warned about neither: the plaintext warning read the probe's own seven-name list, which only the
`--api` branch filled and which — like `manual.SENSITIVE`, `lease_client._SENSITIVE_HEADERS`,
`har_chains.AUTH_HEADERS` and `classify._SECRETISH_NAME` — had never heard of `x-lab-code`,
`passcode`, `access_code` or `session_id`. Five lists, five vocabularies, and the names a real
target uses fell between them. The `env:` hint the CLI already prints for an `Authorization`
literal was the right answer; it was never reached.

Now `runtime/target_secrets.py` holds the rule (`is_secret_name`), `safe_headers` redacts by it,
the warning judges the file's own headers by it, and the classifier, the probe, the HAR report,
the printed-config mask and the capture file all ask it. What the PLATFORM record carries is
unchanged: the header still goes on the application, value intact — that is what reaches the
target.

The negatives are the substance, not filler: a rule that withheld `Idempotency-Key`,
`X-Country-Code` or `token_types` would 401 a target or blind an operator.
"""
import inspect
import json
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
for _p in (REPO / "shells" / "cli", REPO / "runtime", REPO / "control", REPO):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
import ascend  # noqa: E402
import manual  # noqa: E402
import target_secrets as TS  # noqa: E402
from discovery import classify as C  # noqa: E402
from lease_client import LeaseClient  # noqa: E402
from runtime.discovery import codegen  # noqa: E402
from runtime.discovery import har_chains as HC  # noqa: E402
from runtime.discovery import probe as P  # noqa: E402
from _onboard_harness import FakePlatform, run_target_add  # noqa: E402

# The lab shape, with STAND-IN values: the credential under two names, a country code that only
# looks like one, and the content type.
LAB = {
    "Content-Type": "application/json",
    "Cookie": "lab_access=1; sid=standin-cookie-9f13c2",
    "x-lab-code": "standin-lab-code-9f13c2",
    "X-Country-Code": "US",
}
CFG = {"adapter": "direct_api", "endpoint": "https://lab.example.com/session/api/messages",
       "method": "POST", "body": {"message": "{{PROMPT}}"}, "response_path": "reply",
       "headers": {"Content-Type": "application/json"}}


@pytest.fixture(autouse=True)
def _public_target(monkeypatch):
    """The transport choice asks DNS whether the target is public; the harness poisons DNS."""
    monkeypatch.setattr(ascend, "_is_public_host", lambda url: True)


def ns(**kw):
    base = dict(header=None, bearer=None, token_file=None, api_key=None, basic=None, cookie=None,
                allow_internal=False, _login_auth=None)
    base.update(kw)
    return types.SimpleNamespace(**base)


def _hdrs(record):
    """A record's headers as a dict: the create spec carries them as [{name, value}] on the wire."""
    h = record.get("headers") or {}
    return {x["name"]: x["value"] for x in h} if isinstance(h, list) else dict(h)


def _har(headers):
    return {"log": {"version": "1.2", "entries": [{
        "startedDateTime": "2026-10-09T00:00:00.000Z", "time": 5,
        "request": {"method": "POST", "url": "https://lab.example.com/session/api/messages",
                    "httpVersion": "HTTP/1.1", "queryString": [], "cookies": [],
                    "headersSize": -1, "bodySize": 30,
                    "headers": [{"name": k, "value": v} for k, v in headers.items()],
                    "postData": {"mimeType": "application/json", "text": '{"message":"hi"}'}},
        "response": {"status": 200, "statusText": "OK", "httpVersion": "HTTP/1.1",
                     "headers": [{"name": "Content-Type", "value": "application/json"}],
                     "cookies": [], "redirectURL": "", "headersSize": -1, "bodySize": 24,
                     "content": {"size": 24, "mimeType": "application/json",
                                 "text": '{"reply":"hello"}'}},
        "cache": {}, "timings": {"send": 0, "wait": 0, "receive": 0}}]}}


class TestTheRule:
    @pytest.mark.parametrize("name", [
        "authorization", "Proxy-Authorization", "cookie", "Set-Cookie",
        "api-key", "apikey", "X-API-Key", "token", "X-Auth-Token", "x-amz-security-token",
        "secret", "client_secret", "password", "passwd", "credential", "bearer",
        "signature", "x-hmac-sha256", "passcode",
        "access_code", "x-lab-code", "auth_code", "api-code", "app_code", "client-code", "invite_code",
        "session-id", "session_id", "sid",
    ])
    def test_a_credential_name(self, name):
        assert TS.is_secret_name(name) is True

    @pytest.mark.parametrize("name", [
        "country_code", "content-type", "accept", "x-request-id", "user-agent",
        "x-requested-with", "traceparent", "x-correlation-id", "idempotency-key", "x-channel",
        "auth_lifecycle", "session_header", "token_types", "x-ratelimit-remaining-tokens",
    ])
    def test_not_a_credential_name(self, name):
        assert TS.is_secret_name(name) is False

    @pytest.mark.parametrize("spelling", ["x-lab-code", "X-LAB-CODE", "x_lab_code", "x.lab.code", "xLabCode"])
    def test_case_and_separators_do_not_matter(self, spelling):
        assert TS.is_secret_name(spelling) is True

    def test_a_credential_word_must_stand_on_a_boundary(self):
        assert TS.is_secret_name("countrycode") is False       # no access/lab/auth/… before `code`
        assert TS.is_secret_name("accesstoken") is True        # compounds run together like field names
        assert TS.is_secret_name("sessionId") is True

    def test_bare_code_key_and_session_count_only_as_query_parameters(self):
        for n in ("code", "key", "session"):
            assert TS.is_secret_name(n) is False and TS.is_secret_param_name(n) is True
        assert TS.is_secret_param_name("country_code") is False

    def test_the_rule_never_raises(self):
        for junk in (None, 42, "", b"x", {"a": 1}):
            assert TS.is_secret_name(junk) is False


class TestSafeHeadersRedactsByTheRule:
    def test_the_lab_shape_is_masked_under_both_credential_names(self):
        kept, dropped = codegen.safe_headers(LAB)
        assert dropped == []
        assert list(kept) == list(LAB), "order is the config's; a mask must not reshuffle it"
        assert kept["Cookie"] == "[REDACTED]" and kept["x-lab-code"] == "[REDACTED]"
        assert kept["X-Country-Code"] == "US" and kept["Content-Type"] == "application/json"
        assert "9f13c2" not in json.dumps(kept)

    def test_the_format_is_the_one_every_other_mask_uses(self):
        kept, _ = codegen.safe_headers(LAB)
        assert kept["Cookie"] == TS.REDACTED == manual.redact({"Cookie": "x"})["Cookie"]

    def test_the_wire_form_keeps_the_value_the_target_needs(self):
        kept, _ = codegen.safe_headers(LAB, redact=False)
        assert kept == LAB

    def test_fingerprint_and_query_rules_hold_in_both_forms(self):
        h = {**LAB, "Sec-Ch-Ua": '"Chromium";v="131"', "Referer": "https://h/frame?code=SECRET"}
        for redact in (True, False):
            kept, dropped = codegen.safe_headers(h, redact=redact)
            assert dropped == ["Sec-Ch-Ua"] and kept["Referer"] == "https://h/frame"


class TestThePlatformRecordStillCarriesTheCredential:
    def test_the_direct_contract_is_the_wire_form(self):
        out = ascend._api_contract({**CFG, "headers": LAB})
        assert out["headers"]["Cookie"] == LAB["Cookie"]
        assert out["headers"]["x-lab-code"] == LAB["x-lab-code"]

    def test_the_adaptor_app_is_created_with_the_literal(self, monkeypatch, tmp_path):
        platform = FakePlatform()
        run_target_add(monkeypatch, tmp_path, {**CFG, "headers": LAB}, platform, name="lab-session")
        (spec,) = platform.created
        assert _hdrs(spec)["x-lab-code"] == LAB["x-lab-code"]
        assert _hdrs(spec)["Cookie"] == LAB["Cookie"]

    def test_the_deprecated_module_keeps_its_literal_headers(self):
        """custom_module.py calls send_prompt(prompt) and nothing else: a masked value would 401."""
        src = codegen.generate_adapter_module("lab", {**CFG, "headers": LAB})
        assert LAB["x-lab-code"] in src


class TestThePlaintextHint:
    @pytest.mark.parametrize("name", ["x-lab-code", "Cookie", "Authorization"])
    def test_a_literal_under_a_credential_name_gets_the_env_hint(self, name, capsys):
        ascend._finalize_target_auth({"headers": {name: "standin-literal", "X-Country-Code": "US"}}, ns())
        err = capsys.readouterr().err
        assert "plaintext" in err and name in err
        assert f"--header '{name}: env:MY_SECRET'" in err
        assert "X-Country-Code" not in err

    def test_no_probe_list_is_needed(self, capsys):
        """The `--config` re-run and the HAR branch never fill `_probe.inline_secret_headers`."""
        ascend._finalize_target_auth({"headers": {"x-lab-code": "standin"}}, ns())
        assert "x-lab-code" in capsys.readouterr().err

    @pytest.mark.parametrize("value", ["env:LAB_CODE", "Bearer {{TOKEN}}"])
    def test_a_reference_or_a_template_is_not_a_literal(self, value, capsys):
        ascend._finalize_target_auth({"headers": {"x-lab-code": value}}, ns())
        assert "plaintext" not in capsys.readouterr().err

    def test_a_non_credential_header_is_not_warned_about(self, capsys):
        ascend._finalize_target_auth(
            {"headers": {"X-Country-Code": "US", "Content-Type": "application/json"}}, ns())
        assert "plaintext" not in capsys.readouterr().err

    def test_the_lab_rerun_prints_it_and_still_registers_the_header(self, monkeypatch, tmp_path, capsys):
        """The measured command, end to end: the hint on stderr, the literal on the record."""
        monkeypatch.setenv("ASCEND_CONFIG_DIR", str(tmp_path))
        platform = FakePlatform()
        run_target_add(monkeypatch, tmp_path, CFG, platform, name="lab-session",
                       header=["x-lab-code: standin-lab-code-9f13c2"])
        err = capsys.readouterr().err
        assert "credential-shaped header(s) stored in plaintext in the config: x-lab-code" in err
        assert "--header 'x-lab-code: env:MY_SECRET'" in err
        (spec,) = platform.created
        assert _hdrs(spec)["x-lab-code"] == "standin-lab-code-9f13c2"


class TestEveryFormerListAsksTheRule:
    def test_the_printed_config_mask(self):
        out = manual.redact({"headers": LAB, "body": {"passcode": "1234", "country_code": "US"}})
        assert out["headers"]["x-lab-code"] == "[REDACTED]" and out["headers"]["Cookie"] == "[REDACTED]"
        assert out["headers"]["X-Country-Code"] == "US"
        assert out["body"] == {"passcode": "[REDACTED]", "country_code": "US"}

    def test_the_host_call_trace_mask(self):
        text = ascend._mask_urls({"url": "wss://lab.example.com/demo", "opts": {"headers": LAB}})
        assert "9f13c2" not in text and "'X-Country-Code': 'US'" in text

    def test_the_capture_file(self):
        lc = LeaseClient.__new__(LeaseClient)                    # no network, just the redactor
        out = lc._redact({"headers": LAB, "nested": [{"session_id": "s-1", "status": "ok"}]})
        assert out["headers"]["x-lab-code"] == "[REDACTED]" and out["headers"]["Cookie"] == "[REDACTED]"
        assert out["headers"]["X-Country-Code"] == "US"
        assert out["nested"] == [{"session_id": "[REDACTED]", "status": "ok"}]

    def test_the_classifier_withholds_it_from_the_config(self):
        assert C._looks_secret_header("x-lab-code", "standin") is True
        assert C._looks_secret_header("x-country-code", "US") is False
        assert C.dropped_secret_headers({"x-lab-code": "standin", "x-country-code": "US"}) == ["X-Lab-Code"]
        assert C._nonsecret_headers({"x-lab-code": "standin", "x-country-code": "US"}) == {"X-Country-Code": "US"}

    def test_the_probe_flags_it(self):
        r = P.ProbeResult(ok=True, endpoint="https://lab.example.com/session/api/messages", method="POST",
                          request_body={"message": "{{PROMPT}}"}, response_path="reply",
                          response_text="hello", diagnosis="ok", headers=dict(LAB))
        cfg = P.build_config(r)
        assert sorted(cfg["_probe"]["inline_secret_headers"]) == ["Cookie", "x-lab-code"]

    def test_the_har_report(self, tmp_path):
        har = tmp_path / "lab.har"
        har.write_text(json.dumps(_har(LAB)))
        rep = HC.read_chains(str(har))
        (row,) = rep["sequence"]
        assert {a["name"] for a in row["auth"]} == {"Cookie", "x-lab-code"}
        assert "9f13c2" not in json.dumps(rep)


class TestThereIsOneRule:
    def test_the_private_lists_are_gone(self):
        for mod, names in ((C, ("_SECRETISH_NAME", "_SECRET_HEADERS", "_CSRF_HEADERS", "_SECRET_PARAM_NAMES")),
                           (manual, ("SENSITIVE", "SENSITIVE_FIELDS", "_SENSITIVE_NORMALISED")),
                           (LeaseClient, ("_SENSITIVE_HEADERS",)),
                           (HC, ("AUTH_HEADERS",))):
            for n in names:
                assert not hasattr(mod, n), f"{mod.__name__}.{n} is a second vocabulary"

    def test_the_cli_warning_asks_the_rule_not_the_probe(self):
        src = inspect.getsource(ascend._finalize_target_auth)
        assert "is_secret_name(" in src and "inline_secret_headers" not in src

    def test_the_platform_callers_ask_for_the_wire_form(self):
        for fn in (ascend._api_contract, ascend._adaptor_app_headers):
            src = inspect.getsource(fn)
            assert src.count("safe_headers(") == src.count(", redact=False)") >= 1

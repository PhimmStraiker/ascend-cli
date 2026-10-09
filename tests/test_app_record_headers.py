"""
test_app_record_headers.py — a browser's fingerprint headers never reach an application record.

MEASURED 2026-10-09. A config derived from a Target Lab HAR carried the browser's whole header
set: Accept, Origin, Referer, Sec-Fetch-Dest/Mode/Site, User-Agent, Sec-Ch-Ua,
Sec-Ch-Ua-Mobile, Sec-Ch-Ua-Platform. `POST /ascend/applications` with those on the record
answered 400 "the request was rejected by the upstream service"; the same config with them
removed created the app in 4 s. The old Python codegen already had the rule for a module it
generated (`safe_headers`: drop `Sec-Ch-Ua*` and `Sec-Fetch-*` and transport noise, cut a query
string from Referer/Origin because on a widget behind an access code it IS the credential).
The direct application the hosted-adaptor default builds now goes through the same rule.
"""
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
for _p in (REPO / "shells" / "cli", REPO / "runtime", REPO / "control", REPO):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
import ascend  # noqa: E402
from runtime.discovery import codegen  # noqa: E402
from _onboard_harness import FakePlatform, run_target_add  # noqa: E402


@pytest.fixture(autouse=True)
def _public_target(monkeypatch):
    """The transport choice asks DNS whether the target is public; the harness poisons DNS."""
    monkeypatch.setattr(ascend, "_is_public_host", lambda url: True)

CAPTURED = {
    "Accept": "*/*",
    "Content-Type": "application/json",
    "Origin": "https://lab.example.com",
    "Referer": "https://lab.example.com/session/frame?code=lab-code-9f13c2",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "User-Agent": "Mozilla/5.0 (Macintosh) AppleWebKit/537.36 HeadlessChrome/131.0 Safari/537.36",
    "Sec-Ch-Ua": '"HeadlessChrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"macOS"',
    "Content-Length": "44",
    "Host": "lab.example.com",
}
FINGERPRINT = {"Sec-Fetch-Dest", "Sec-Fetch-Mode", "Sec-Fetch-Site", "Sec-Ch-Ua",
               "Sec-Ch-Ua-Mobile", "Sec-Ch-Ua-Platform", "Content-Length", "Host"}
CFG = {"adapter": "direct_api", "endpoint": "https://lab.example.com/session/api/messages",
       "method": "POST", "body": {"message": "{{PROMPT}}"}, "response_path": "reply",
       "headers": CAPTURED}


def _hdrs(record):
    """A record's headers as a dict: the create spec carries them as [{name, value}] on the wire."""
    h = record.get("headers") or {}
    return {x["name"]: x["value"] for x in h} if isinstance(h, list) else dict(h)


class TestTheRule:
    def test_fingerprint_and_noise_are_dropped_the_rest_kept_in_order(self):
        kept, dropped = codegen.safe_headers(CAPTURED)
        assert set(dropped) == FINGERPRINT
        assert list(kept) == ["Accept", "Content-Type", "Origin", "Referer", "User-Agent"]
        assert kept["User-Agent"] == CAPTURED["User-Agent"], "a target may check the UA; it stays"

    def test_the_query_string_is_cut_from_referer_and_origin(self):
        kept, _ = codegen.safe_headers({"Referer": "https://h/session/frame?code=SECRET",
                                        "Origin": "https://h?code=SECRET"})
        assert kept == {"Referer": "https://h/session/frame", "Origin": "https://h"}
        assert "SECRET" not in json.dumps(kept)

    @pytest.mark.parametrize("name", ["sec-ch-ua-arch", "Sec-Fetch-User", "SEC-CH-UA-FULL-VERSION-LIST"])
    def test_the_rule_is_a_prefix_match_in_any_case(self, name):
        kept, dropped = codegen.safe_headers({name: "x", "X-Channel": "web"})
        assert dropped == [name] and kept == {"X-Channel": "web"}

    def test_the_old_codegen_module_applies_it(self):
        src = codegen.generate_adapter_module("lab", CFG)
        assert "Sec-Ch-Ua" not in src and "Sec-Fetch" not in src
        assert '"User-Agent"' in src and "lab-code-9f13c2" not in src


class TestTheDirectApplication:
    def test_api_contract_carries_no_fingerprint_header(self):
        out = ascend._api_contract(CFG)
        assert not FINGERPRINT & set(out["headers"])
        assert out["headers"]["Content-Type"] == "application/json"
        assert out["headers"]["Referer"] == "https://lab.example.com/session/frame"

    def test_the_adaptor_app_is_created_without_them(self, monkeypatch, tmp_path):
        platform = FakePlatform()
        run_target_add(monkeypatch, tmp_path, CFG, platform, name="lab-session")
        (spec,) = platform.created
        assert not FINGERPRINT & set(_hdrs(spec)), _hdrs(spec)
        assert _hdrs(spec)["Accept"] == "*/*" and _hdrs(spec)["Origin"] == "https://lab.example.com"
        assert "lab-code-9f13c2" not in json.dumps(spec)

    def test_a_re_registration_patches_them_off_the_existing_record(self, monkeypatch, tmp_path):
        existing = {"id": "aapp_old", "name": "lab-session", "api_type": "api",
                    "url": CFG["endpoint"], "headers": CAPTURED,
                    "request_template": json.dumps({"message": "{{PROMPT}}", "_adaptor_src": "OLD"})}
        platform = FakePlatform(existing=existing)
        run_target_add(monkeypatch, tmp_path, CFG, platform, name="lab-session")
        ((_, patch),) = [p for p in platform.patches if "headers" in p[1]][:1]
        assert not FINGERPRINT & set(_hdrs(patch))

    def test_with_a_captured_credential_the_filter_still_holds(self, monkeypatch, tmp_path):
        """The matrix cell that shipped broken: fingerprint headers AND an auth block. `merge_auth`
        hands back the config's own headers plus the credential, so merging the unfiltered config
        put every dropped header straight back on the record. Measured live, three rejects."""
        monkeypatch.setenv("ASCEND_SECRET_LAB_EXAMPLE_COM_COOKIE", "lab_access=1; nw_consent=yes")
        cfg = {**CFG, "auth": {"type": "static", "mode": "headers",
                               "headers": {"Cookie": "env:ASCEND_SECRET_LAB_EXAMPLE_COM_COOKIE"}}}
        platform = FakePlatform()
        run_target_add(monkeypatch, tmp_path, cfg, platform, name="lab-session")
        (spec,) = platform.created
        got = _hdrs(spec)
        assert not FINGERPRINT & set(got), got
        assert got["Cookie"] == "lab_access=1; nw_consent=yes"
        assert got["User-Agent"] == CAPTURED["User-Agent"]

    def test_the_output_names_what_was_left_off(self, monkeypatch, tmp_path, capsys):
        run_target_add(monkeypatch, tmp_path, CFG, FakePlatform(), name="lab-session")
        err = capsys.readouterr().err
        assert "left off the record" in err and "Sec-Ch-Ua" in err

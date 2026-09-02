"""
test_secret_redaction — credentials must not reach stdout, logs, capture files or the platform.

Both cases here were live defects found in a pre-release review, and both were invisible because
the thing that leaked did not look like a secret at the point it leaked:

  * the platform returns `thin_api_key` on GET and in the app LIST, not only at creation, so a
    read-only `app list` printed every bridge-type app's key in full;
  * redaction matched on key NAMES only, while this tool itself bakes credentials into a URL query
    string (`--api-key ...:in=query`, a Gemini-style `?key=`), so the credential rode through
    masking in a value and was logged, captured, displayed and posted upstream.
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
for p in ("control", "runtime", "shells/cli"):
    sys.path.insert(0, str(REPO / p))
import ascend      # noqa: E402
import manual      # noqa: E402


# ---- the bridge key must never be printed by a read command -----------------------------------
def test_app_record_key_is_masked():
    app = {"id": "aapp_x", "name": "bot", "api_type": "thin", "thin_api_key": "tc-abcdef123456"}
    out = ascend._mask_app(app)
    assert out["thin_api_key"] != "tc-abcdef123456"
    assert "tc-abcdef123456" not in str(out)
    assert out["id"] == "aapp_x" and out["name"] == "bot"      # everything else survives


def test_masking_is_safe_on_records_without_a_key():
    for app in ({"id": "aapp_y", "api_type": "api"}, {}, None, "not-a-dict"):
        ascend._mask_app(app)                                   # must not raise


# ---- a credential in a URL query string is a credential ----------------------------------------
def test_url_query_credentials_are_masked():
    for url, secret in (
        ("https://api.example.com/v1:generateContent?key=AIzaSyTOPSECRET", "AIzaSyTOPSECRET"),
        ("https://h/x?api_key=SEKRIT&page=2", "SEKRIT"),
        ("https://h/x?access_token=SEKRIT", "SEKRIT"),
        ("https://h/x?token=SEKRIT", "SEKRIT"),
    ):
        assert secret not in manual.redact_url(url), url


def test_benign_query_parameters_survive():
    out = manual.redact_url("https://h/chat?model=gpt-4&stream=true")
    assert "model=gpt-4" in out and "stream=true" in out


def test_redact_covers_urls_nested_in_a_config():
    cfg = {"endpoint": "https://h/chat?key=SEKRIT", "headers": {"Authorization": "Bearer T0KEN"}}
    out = manual.redact(cfg)
    assert "SEKRIT" not in str(out)          # the URL credential
    assert "T0KEN" not in str(out)           # and the header one


def test_non_urls_and_odd_values_pass_through_unchanged():
    for v in ("just a sentence with ? in it", "", None, 42, ["a"], {"k": "v"}):
        manual.redact_url(v)                                     # must not raise
    assert manual.redact_url("no-scheme.example?key=x") == "no-scheme.example?key=x"


def test_capture_redaction_also_covers_urls():
    from lease_client import LeaseClient
    lc = LeaseClient.__new__(LeaseClient)                        # no network, just the redactor
    out = lc._redact({"url": "https://h/x?key=SEKRIT", "headers": {"authorization": "Bearer T"}})
    assert "SEKRIT" not in str(out)
    assert out["headers"]["authorization"] == "[REDACTED]"

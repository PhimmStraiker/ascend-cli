"""
A `{"type": "static", "mode": "headers"}` auth block resolves to the headers it names.

`discovery.classify` writes this block for every credential a capture presented (one `env:`
reference per header) and `dispatch.merge_auth` folds auth into every outbound request — but the
static materializer knew bearer/api_key/basic/cookie/custom and refused "headers", so a captured
credential could never authenticate a run. The adaptor path needs the same resolution to put the
literal values on the application record.
"""
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
for p in ("runtime", "control"):
    if str(REPO / p) not in sys.path:
        sys.path.insert(0, str(REPO / p))
from layers.auth import AuthError, AuthProvider  # noqa: E402
import dispatch  # noqa: E402


def test_every_named_header_is_resolved(monkeypatch):
    monkeypatch.setenv("ASCEND_SECRET_T_X_LAB_CODE", "lab-1234")
    monkeypatch.setenv("ASCEND_SECRET_T_COOKIE", "sid=abc")
    mat = AuthProvider({"type": "static", "mode": "headers",
                        "headers": {"x-lab-code": "env:ASCEND_SECRET_T_X_LAB_CODE",
                                    "Cookie": "env:ASCEND_SECRET_T_COOKIE"}}).materialize()
    assert mat.headers == {"x-lab-code": "lab-1234", "Cookie": "sid=abc"}


def test_an_unset_reference_fails_naming_the_variable(monkeypatch):
    monkeypatch.delenv("ASCEND_SECRET_T_MISSING", raising=False)
    with pytest.raises(AuthError, match="ASCEND_SECRET_T_MISSING"):
        AuthProvider({"type": "static", "mode": "headers",
                      "headers": {"x-k": "env:ASCEND_SECRET_T_MISSING"}}).materialize()


def test_merge_auth_folds_them_into_the_config_headers(monkeypatch):
    monkeypatch.setenv("ASCEND_SECRET_T_X_LAB_CODE", "lab-1234")
    cfg = {"endpoint": "https://t.example.com/chat", "headers": {"Content-Type": "application/json"},
           "auth": {"type": "static", "mode": "headers",
                    "headers": {"x-lab-code": "env:ASCEND_SECRET_T_X_LAB_CODE"}}}
    merged = dispatch.merge_auth(cfg)
    assert "_auth_error" not in merged
    assert merged["headers"] == {"Content-Type": "application/json", "x-lab-code": "lab-1234"}


def test_a_list_of_blocks_still_merges_every_credential(monkeypatch):
    """Two env references (a passcode header and a bearer) are an auth LIST; both must land."""
    monkeypatch.setenv("T_CODE", "code-1")
    monkeypatch.setenv("T_TOKEN", "tok-1")
    cfg = {"auth": [{"type": "static", "mode": "custom", "name": "x-lab-code", "value_ref": "env:T_CODE",
                     "template": "{{VALUE}}"},
                    {"type": "static", "mode": "bearer", "value_ref": "env:T_TOKEN"}]}
    merged = dispatch.merge_auth(cfg)
    assert merged["headers"] == {"x-lab-code": "code-1", "Authorization": "Bearer tok-1"}

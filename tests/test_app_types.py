"""
The four application types the platform accepts, and the fields each one needs.

`POST /ascend/applications` is a discriminated union on `api_type` (verified against the live
OpenAPI document). Only `thin` needs a locally-running bridge; `api`/`gcp`/`bedrock` are called
by Ascend directly, which is why they must never trigger the NO-BRIDGE alarm.

What these tests protect:
  - a missing per-type field is named locally, never relayed as a 422
  - only `thin` is treated as needing a bridge
  - `critical` is clamped to `high`, because the platform's category enum stops there
  - templates go out as JSON strings and headers as an array, which is what v3 wants
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "control"))

import api  # noqa: E402


class TestTypeCoverage:
    def test_all_four_platform_types_are_supported(self):
        assert set(api.API_TYPES) == {"api", "thin", "gcp", "bedrock"}
        assert set(api.REQUIRED_BY_TYPE) == set(api.API_TYPES)

    def test_unknown_type_is_rejected_with_the_choices(self):
        with pytest.raises(api.SpecError, match="api, thin, gcp, bedrock"):
            api.build_app_spec(name="x", api_type="lambda")


class TestRequiredFields:
    """Each per-type requirement matches the API contract and fails locally, naming the gap."""

    def test_api_needs_url_and_key(self):
        with pytest.raises(api.SpecError) as e:
            api.build_app_spec(name="x", api_type="api")
        assert "url" in str(e.value) and "api_key" in str(e.value)

    def test_api_is_complete_with_url_and_key(self):
        spec = api.build_app_spec(name="x", api_type="api", url="https://t/x", api_key="k")
        assert spec["api_type"] == "api"
        assert spec["url"] == "https://t/x"

    def test_thin_needs_no_url(self):
        spec = api.build_app_spec(name="x", api_type="thin")
        assert spec["api_type"] == "thin"
        assert "url" not in spec

    def test_gcp_needs_service_account(self):
        with pytest.raises(api.SpecError, match="service_account_info"):
            api.build_app_spec(name="x", api_type="gcp", url="https://t/x")
        spec = api.build_app_spec(name="x", api_type="gcp", url="https://t/x",
                                  service_account_info='{"type":"service_account"}')
        assert spec["service_account_info"]

    def test_bedrock_needs_an_auth_method(self):
        with pytest.raises(api.SpecError, match="bedrock_authentication_method"):
            api.build_app_spec(name="x", api_type="bedrock", url="arn:aws:bedrock:x")
        spec = api.build_app_spec(name="x", api_type="bedrock", url="arn:aws:bedrock:x",
                                  bedrock_authentication_method="assume-role",
                                  role_arn="arn:aws:iam::1:role/r", region="us-east-1")
        assert spec["role_arn"] and spec["region"] == "us-east-1"

    def test_bedrock_omits_credential_fields_it_was_not_given(self):
        spec = api.build_app_spec(name="x", api_type="bedrock", url="arn:x",
                                  bedrock_authentication_method="access-key",
                                  access_key_id="AKIA", secret_access_key="s")
        assert "role_arn" not in spec and "session_token" not in spec


class TestWireShape:
    """v3 is particular about how templates and headers are encoded."""

    def test_templates_are_json_strings_and_headers_an_array(self):
        spec = api.build_app_spec(name="x", api_type="thin",
                                  request_template={"q": "{{PROMPT}}"},
                                  response_template={"a": "{{RESPONSE}}"},
                                  headers={"Authorization": "Bearer t"})
        assert isinstance(spec["request_template"], str)
        assert json.loads(spec["request_template"]) == {"q": "{{PROMPT}}"}
        assert spec["headers"] == [{"name": "Authorization", "value": "Bearer t"}]

    def test_a_string_template_is_passed_through_unchanged(self):
        spec = api.build_app_spec(name="x", api_type="thin",
                                  request_template='{"q":"{{PROMPT}}"}')
        assert spec["request_template"] == '{"q":"{{PROMPT}}"}'

    def test_control_type_reflects_whether_controls_were_named(self):
        assert api.build_app_spec(name="x")["control_type"] == "all"
        assert api.build_app_spec(name="x", control_ids=["a"])["control_type"] == "custom"

    def test_system_prompt_defaults_to_the_name(self):
        """The scorer compares responses against system_prompt to detect a leak, so an empty
        one silently weakens system-prompt-leak detection."""
        assert api.build_app_spec(name="My Bot")["system_prompt"] == "My Bot"


class TestBridgeRequirement:
    def test_only_thin_needs_a_bridge(self):
        assert api.needs_bridge({"api_type": "thin"}) is True
        for t in ("api", "gcp", "bedrock"):
            assert api.needs_bridge({"api_type": t}) is False, \
                f"{t} is called by Ascend directly — flagging it trains people to ignore the alarm"

    def test_unknown_or_missing_type_does_not_claim_a_bridge(self):
        assert api.needs_bridge({}) is False
        assert api.needs_bridge(None) is False
        assert api.needs_bridge({"api_type": "THIN"}) is True, "case must not matter"


class TestCategorySeverity:
    def test_dict_and_pairs_both_normalize_to_the_api_shape(self):
        want = [{"id": "data_leak", "severity": "high"}]
        assert api.normalize_category_severities({"data_leak": "high"}) == want
        assert api.normalize_category_severities([("data_leak", "high")]) == want
        assert api.normalize_category_severities(
            [{"id": "data_leak", "severity": "high"}]) == want

    def test_critical_is_clamped_because_the_platform_enum_stops_at_high(self):
        got = api.normalize_category_severities({"data_leak": "critical"})
        assert got == [{"id": "data_leak", "severity": "high"}]
        assert api.clamped_severities({"data_leak": "critical"}) == ["data_leak"]

    def test_clamping_is_reported_so_it_is_never_silent(self):
        assert api.clamped_severities({"a": "high", "b": "critical"}) == ["b"]

    def test_an_unknown_severity_is_refused(self):
        with pytest.raises(api.SpecError, match="default, low, medium, high"):
            api.normalize_category_severities({"data_leak": "extreme"})

    def test_severities_ride_along_on_the_spec(self):
        spec = api.build_app_spec(name="x", category_severities={"data_leak": "medium"})
        assert spec["category_severities"] == [{"id": "data_leak", "severity": "medium"}]


class TestInputGuardrails:
    def test_both_platform_types_are_accepted(self):
        assert api.build_input_guardrails(type="http_status_code", value="403") == {
            "input_guardrails_enabled": True,
            "input_guardrails_type": "http_status_code",
            "input_guardrails_value": ["403"]}
        g = api.build_input_guardrails(type="response_pattern", value=["nope", "denied"])
        assert g["input_guardrails_value"] == ["nope", "denied"]

    def test_an_unknown_type_is_refused(self):
        with pytest.raises(api.SpecError, match="http_status_code, response_pattern"):
            api.build_input_guardrails(type="magic", value="x")

    def test_guardrails_ride_along_on_the_spec(self):
        spec = api.build_app_spec(name="x",
                                  input_guardrails={"type": "http_status_code", "value": "403"})
        assert spec["input_guardrails_type"] == "http_status_code"


class TestPlaceholderHygiene:
    def test_spaced_placeholders_are_normalized_on_the_way_out(self):
        """`{{ PROMPT }}` silently never substitutes — the platform matches the exact token."""
        cleaned = api._clean_templates({"request_template": '{"q":"{{ PROMPT }}"}'})
        assert cleaned["request_template"] == '{"q":"{{PROMPT}}"}'

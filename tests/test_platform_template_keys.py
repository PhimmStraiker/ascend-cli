"""
test_platform_template_keys — re-registering a direct app keeps the keys the platform wrote.

When `target add` lands on an application that already exists, a direct (`api`) app has its
request_template refreshed in place from the new capture — that is what makes a fixed header or a
rotated key an update rather than a twin. The stored template can also carry keys the assessment
engine put there for itself (`_adaptor_*`, `_adapter_*`, `_tunnel_*`, `_iris_*`); it strips them
before anything reaches the target, so a capture of the target's real client never contains them.
A refresh that sent the capture alone therefore erased the platform's own wiring, silently, with
the app still reporting a perfectly valid contract.

The rule: the capture decides the contract, and only the platform's keys survive from the stored
template — a key the capture sets wins, an operator field the capture dropped stays dropped.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
for _p in (REPO / "shells" / "cli", REPO / "runtime", REPO / "control", REPO):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
import ascend  # noqa: E402
from _onboard_harness import FakePlatform, run_target_add  # noqa: E402

keep = ascend._keep_platform_template_keys

STORED = {"message": "{{PROMPT}}", "workspace": "retail-support",
          "_adaptor_src": "console-capture-2026-09", "_tunnel_agent_keys": ["tk_a", "tk_b"],
          "_adapter_mode": "replay", "_iris_run": 7, "legacy_field": "dropped-by-the-operator"}
FRESH = {"message": "{{PROMPT}}", "workspace": "retail-support"}


class TestTheRule:
    def test_every_platform_prefix_is_carried_when_the_capture_does_not_set_it(self):
        out = keep(STORED, FRESH)
        assert out["_adaptor_src"] == "console-capture-2026-09"
        assert out["_tunnel_agent_keys"] == ["tk_a", "tk_b"]
        assert out["_adapter_mode"] == "replay"
        assert out["_iris_run"] == 7
        assert out["message"] == "{{PROMPT}}" and out["workspace"] == "retail-support"

    def test_an_operator_field_the_capture_dropped_stays_dropped(self):
        assert "legacy_field" not in keep(STORED, FRESH)

    def test_a_key_the_capture_sets_itself_wins(self):
        out = keep(STORED, {**FRESH, "_adaptor_src": "curl-2026-10"})
        assert out["_adaptor_src"] == "curl-2026-10"

    def test_the_wire_shape_is_read_too(self):
        """GET returns the template as the JSON string the platform stores, not a dict."""
        out = keep(json.dumps(STORED), FRESH)
        assert out["_adaptor_src"] == "console-capture-2026-09"
        assert out["_tunnel_agent_keys"] == ["tk_a", "tk_b"]

    def test_the_prefixes_are_the_four_the_platform_owns(self):
        assert set(ascend.PLATFORM_TEMPLATE_PREFIXES) == {"_adaptor_", "_adapter_", "_tunnel_", "_iris_"}

    @pytest.mark.parametrize("stored", [None, "", "not json", 42, {}])
    def test_nothing_to_carry_leaves_the_capture_untouched(self, stored):
        assert keep(stored, FRESH) == FRESH

    def test_a_template_the_capture_did_not_send_is_not_invented(self):
        assert keep(STORED, None) is None


def _direct_app(template):
    return {"id": "aapp_retail", "name": "Retail Bot", "api_type": "api",
            "url": "https://8.8.8.8/api/chat", "request_template": template}


# The config a re-run derives from a fresh capture: the same contract, none of the platform's keys.
RECAPTURED = {"adapter": "direct_api", "endpoint": "https://8.8.8.8/api/chat", "method": "POST",
              "body": {"message": "{{PROMPT}}", "workspace": "retail-support"},
              "response_path": "message", "headers": {"x-demo-key": "pass"}}


class TestARerunOfTargetAdd:
    def test_keeps_the_stored_adaptor_src_and_tunnel_agent_keys(self, monkeypatch, tmp_path, capsys):
        """The whole path: an existing direct app, a fresh capture, one PATCH — and the platform's
        keys are in it. The template arrives from GET in the wire shape (a JSON string)."""
        platform = FakePlatform(existing=_direct_app(json.dumps(STORED)))
        run_target_add(monkeypatch, tmp_path, RECAPTURED, platform, name="Retail Bot")
        (app_id, patch), = platform.patches
        assert app_id == "aapp_retail" and platform.created == []
        sent = patch["request_template"]
        assert sent["_adaptor_src"] == "console-capture-2026-09"
        assert sent["_tunnel_agent_keys"] == ["tk_a", "tk_b"]
        assert sent["message"] == "{{PROMPT}}" and sent["workspace"] == "retail-support"
        assert "legacy_field" not in sent
        out = json.loads(capsys.readouterr().out)
        assert out["reused"] is True and out["transport"] == "api"

    def test_the_patch_is_the_captures_contract_when_nothing_was_stored(self, monkeypatch, tmp_path):
        platform = FakePlatform(existing=_direct_app({"message": "{{PROMPT}}"}))
        run_target_add(monkeypatch, tmp_path, RECAPTURED, platform, name="Retail Bot")
        (_, patch), = platform.patches
        assert patch["request_template"] == {"message": "{{PROMPT}}", "workspace": "retail-support"}

    def test_the_carry_over_sits_on_the_readopt_patch_itself(self):
        """Source discipline: the one PATCH that refreshes a contract goes through the helper,
        so a second re-wire path cannot be added that forgets it."""
        import inspect
        src = inspect.getsource(ascend.cmd_onboard)
        block = src[src.index("patch = _api_contract(cfg)"):src.index("c.patch_app(app_id, patch)")]
        assert "_keep_platform_template_keys(" in block

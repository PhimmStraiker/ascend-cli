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
    """The plain template path (`--via api`): the app IS its template, so a stored adaptor the
    capture does not set is the platform's wiring and survives."""

    def test_keeps_the_stored_adaptor_src_and_tunnel_agent_keys(self, monkeypatch, tmp_path, capsys):
        """The whole path: an existing direct app, a fresh capture, one PATCH — and the platform's
        keys are in it. The template arrives from GET in the wire shape (a JSON string)."""
        platform = FakePlatform(existing=_direct_app(json.dumps(STORED)))
        run_target_add(monkeypatch, tmp_path, RECAPTURED, platform, name="Retail Bot", via="api")
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
        run_target_add(monkeypatch, tmp_path, RECAPTURED, platform, name="Retail Bot", via="api")
        (_, patch), = platform.patches
        assert patch["request_template"] == {"message": "{{PROMPT}}", "workspace": "retail-support"}


class TestARerunUnderTheAdaptorDefault:
    """The default (`via=auto`) re-wires an existing direct app as an adaptor app: the engine's
    other keys survive, the adaptor is the FRESH one the gate just returned — never the stored
    one, which may be a placeholder or an earlier generation — and the run is proven."""

    def test_the_adaptor_is_fresh_and_the_other_platform_keys_survive(self, monkeypatch, tmp_path, capsys):
        platform = FakePlatform(existing=_direct_app(json.dumps(STORED)))
        run_target_add(monkeypatch, tmp_path, RECAPTURED, platform, name="Retail Bot")
        (app_id, patch), = platform.patches
        assert app_id == "aapp_retail" and platform.created == []
        sent = patch["request_template"]
        assert sent["_adaptor_src"] == "FRESH-ADAPTOR", "never the stored adaptor on a re-add"
        assert sent["_tunnel_agent_keys"] == ["tk_a", "tk_b"]
        assert sent["_adapter_mode"] == "replay" and sent["_iris_run"] == 7
        assert sent["message"] == "{{PROMPT}}" and sent["workspace"] == "retail-support"
        assert "legacy_field" not in sent and "_adaptor_domains" not in sent
        assert patch["url"] == "https://8.8.8.8/api/chat"
        assert patch["headers"]["x-demo-key"] == "pass"
        assert json.loads(patch["response_template"]) == {"message": "{{RESPONSE}}"} if isinstance(
            patch["response_template"], str) else patch["response_template"] == {"message": "{{RESPONSE}}"}
        assert "api_key" not in patch, "the 'none' fallback never reaches an adaptor app"
        out = json.loads(capsys.readouterr().out)
        assert out["reused"] is True and out["transport"] == "adaptor"
        assert out["adaptor"]["verified"] is True and out["console_id"] == platform.console_uuid
        assert platform.gated and platform.tested[0][1] == platform.console_uuid
        assert platform.verified == [(platform.console_uuid, 120.0)]

    def test_a_new_target_is_created_as_an_adaptor_app_and_proven(self, monkeypatch, tmp_path, capsys):
        platform = FakePlatform(existing=None)
        run_target_add(monkeypatch, tmp_path, RECAPTURED, platform, name="Retail Bot")
        (spec,) = platform.created
        assert spec["api_type"] == "api" and spec["url"] == "https://8.8.8.8/api/chat"
        tpl = json.loads(spec["request_template"])
        assert tpl["_adaptor_src"] == "FRESH-ADAPTOR" and tpl["message"] == "{{PROMPT}}"
        assert "api_key" not in spec
        assert {h["name"]: h["value"] for h in spec["headers"]}["x-demo-key"] == "pass"
        out = json.loads(capsys.readouterr().out)
        assert out["transport"] == "adaptor" and out["needs_bridge"] is False
        assert out["adaptor"]["gated"] and out["adaptor"]["tested"] and out["adaptor"]["stored"] and out["adaptor"]["verified"]
        assert out["adaptor"]["shape"] == "direct_api" and out["adaptor"]["source"].endswith("mybot.adaptor.js")
        assert (tmp_path / "mybot.adaptor.js").read_text().count("function sendTurn(turn, host)") == 1
        # the sequence: gate, create, test through the engine, read back, verify
        assert len(platform.gated) == 1 and len(platform.tested) == 1 and len(platform.verified) == 1

    def test_an_env_referenced_credential_lands_on_the_record_and_is_said(self, monkeypatch, tmp_path, capsys):
        """`_finalize_target_auth` moves it out of the headers; the engine cannot read this
        machine's environment, so the literal goes on the app record, and the output says so."""
        monkeypatch.setenv("RETAIL_CODE", "lab-code-value")
        cfg = {**RECAPTURED, "headers": {},
               "auth": {"type": "static", "mode": "custom", "name": "x-demo-key",
                        "value_ref": "env:RETAIL_CODE", "template": "{{VALUE}}"}}
        platform = FakePlatform(existing=None)
        run_target_add(monkeypatch, tmp_path, cfg, platform, name="Retail Bot")
        (spec,) = platform.created
        assert {h["name"]: h["value"] for h in spec["headers"]}["x-demo-key"] == "lab-code-value"
        captured = capsys.readouterr()
        out = json.loads(captured.out)
        assert out["adaptor"]["credentials_on_app"] == ["x-demo-key"]
        assert "placed on the application record" in captured.err

    def test_a_failed_engine_test_leaves_the_app_and_exits_1(self, monkeypatch, tmp_path, capsys):
        bad = {"turns": [{"n": 1, "status_code": 502, "ms": 9, "scored": "", "body": {"error": "x"}}]}
        platform = FakePlatform(existing=None, test=bad)
        with pytest.raises(SystemExit) as e:
            run_target_add(monkeypatch, tmp_path, RECAPTURED, platform, name="Retail Bot")
        assert e.value.code == 1
        assert platform.created and platform.verified == []
        env = json.loads(capsys.readouterr().out)
        assert env["ok"] is False and env["error"]["code"] == "adaptor_test_failed"
        assert "ascend adaptor test" in env["error"]["hint"]

    def test_a_refused_gate_registers_nothing(self, monkeypatch, tmp_path, capsys):
        refused = {"ok": False, "summary": "refused", "gate": {"ok": False, "violations": [
            {"kind": "async", "detail": "x", "line": 1}]}}
        platform = FakePlatform(existing=None, gate=refused)
        with pytest.raises(SystemExit) as e:
            run_target_add(monkeypatch, tmp_path, RECAPTURED, platform, name="Retail Bot")
        assert e.value.code == 2 and platform.created == []

    def test_no_console_uuid_means_registered_but_unproven_and_exit_1(self, monkeypatch, tmp_path, capsys):
        platform = FakePlatform(existing=None, console_uuid=None)
        with pytest.raises(SystemExit) as e:
            run_target_add(monkeypatch, tmp_path, RECAPTURED, platform, name="Retail Bot")
        assert e.value.code == 1
        out = json.loads(capsys.readouterr().out)
        assert out["adaptor"]["verified"] is False and out["console_id"] is None and platform.tested == []

    def test_the_console_id_override_skips_the_lookup(self, monkeypatch, tmp_path, capsys):
        platform = FakePlatform(existing=None, console_uuid=None)
        run_target_add(monkeypatch, tmp_path, RECAPTURED, platform, name="Retail Bot",
                       console_id="01a121e9-b759-741a-9b8c-865730c33ee3")
        assert platform.console_asked == []
        assert platform.tested[0][1] == "01a121e9-b759-741a-9b8c-865730c33ee3"
        assert json.loads(capsys.readouterr().out)["console_id"] == "01a121e9-b759-741a-9b8c-865730c33ee3"

    def test_the_carry_over_sits_on_the_readopt_patch_itself(self):
        """Source discipline: the one PATCH that refreshes a contract goes through the helper,
        so a second re-wire path cannot be added that forgets it."""
        import inspect
        src = inspect.getsource(ascend.cmd_onboard)
        block = src[src.index("patch = _api_contract(cfg)"):src.index("c.patch_app(app_id, patch)")]
        assert "_keep_platform_template_keys(" in block

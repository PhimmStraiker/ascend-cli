"""
test_routed_hosts — a name Straiker reaches itself is settled before any DNS lookup.

A target under `.tun.straiker.ai` or `.pl.straiker.ai` is terminated inside the platform: nothing
runs on the operator's machine, and the name never resolves in public DNS. Every host check used
to ask the resolver first, and a miss read as "private — needs a bridge" — a bridge that, started
here, could never reach the name either. `target inspect`, `target check` and `target add` now
recognise the suffix first and say what is true: reached by Straiker, nothing runs on this machine.

Every test poisons the resolver: a lookup fails the test, it is not mocked to a value.
"""
from __future__ import annotations

import json
import socket
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
for _p in (REPO / "shells" / "cli", REPO / "runtime", REPO / "control", REPO):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
import ascend  # noqa: E402
import creds  # noqa: E402
from runtime.discovery import profiles, validate as V  # noqa: E402
from _onboard_harness import (FakePlatform, no_lookup, never_reached, onboard_args,  # noqa: E402
                              run_target_add)

NOTE = ascend.ROUTED_NOTE
ROUTED = ["https://demo-agent.tun.straiker.ai/api/chat",
          "https://demo-link.pl.straiker.ai/v1/messages"]


@pytest.fixture(autouse=True)
def no_dns(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", no_lookup)


class TestTheHostChecks:
    @pytest.mark.parametrize("url", ROUTED)
    def test_a_routed_name_is_reachable_by_the_platform_without_a_lookup(self, url):
        assert ascend._routed_by_straiker(url) is True
        assert ascend._is_public_host(url) is True

    @pytest.mark.parametrize("url", ["https://demo.example.com/chat", "http://127.0.0.1:8080/chat",
                                     "https://tun.straiker.ai/x", "https://a.tun.straiker.ai.evil.example/x"])
    def test_only_the_two_suffixes_count(self, url):
        assert ascend._routed_by_straiker(url) is False

    def test_the_note_is_the_agreed_sentence(self):
        assert NOTE == "reached by Straiker: nothing runs on this machine"

    @pytest.mark.parametrize("url", ROUTED)
    def test_the_egress_guard_sends_nothing_and_resolves_nothing(self, url):
        assert ascend._guard_egress(url, types.SimpleNamespace(allow_internal=False)) is None


class TestTargetAddNeverProposesABridge:
    def _args(self, **kw):
        return types.SimpleNamespace(**{"via": "auto", **kw})

    @pytest.mark.parametrize("url", ROUTED)
    @pytest.mark.parametrize("adapter,cfg_extra", [
        ("direct_api", {}),
        ("sse_stream", {}),                                   # a protocol the platform cannot speak
        ("direct_api", {"auth": {"type": "oauth2"}}),         # a handshake only a bridge could run
    ])
    def test_the_transport_is_direct_and_the_reason_is_the_note(self, url, adapter, cfg_extra):
        via, why = ascend._choose_transport(self._args(), adapter, {"endpoint": url, **cfg_extra})
        assert via == "api" and why == NOTE

    @pytest.mark.parametrize("url", ROUTED)
    def test_insisting_on_a_bridge_is_refused_with_the_note(self, url, capsys, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["ascend", "--json"])    # _die reads argv for the envelope
        with pytest.raises(SystemExit):
            ascend._choose_transport(self._args(via="bridge", json=True), "direct_api", {"endpoint": url})
        err = json.loads(capsys.readouterr().out)["error"]
        assert err["code"] == "bridge_not_possible" and NOTE in err["message"]

    @pytest.mark.parametrize("url", ROUTED)
    def test_the_whole_command_registers_a_direct_app_and_skips_the_local_gate(
            self, url, monkeypatch, tmp_path, capsys):
        cfg = {"adapter": "direct_api", "endpoint": url, "method": "POST",
               "body": {"message": "{{PROMPT}}"}, "response_path": "message"}
        platform = FakePlatform(existing=None)
        run_target_add(monkeypatch, tmp_path, cfg, platform, gate="poison", name="Routed Bot")
        (spec,), out = platform.created, json.loads(capsys.readouterr().out)
        assert spec["api_type"] == "api" and spec["url"] == url
        assert out["transport"] == "api" and out["transport_reason"] == NOTE
        assert out["needs_bridge"] is False and out["key_stored"] is False
        assert out["validated"] is False, "nothing was proven from here, and the result says so"

    @pytest.mark.parametrize("url", ROUTED)
    def test_probing_a_routed_name_says_the_note_not_a_resolver_error(self, url, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["ascend", "--json"])    # _die reads argv for the envelope
        with pytest.raises(SystemExit):
            ascend.cmd_onboard(onboard_args(api=url, json=True))
        err = json.loads(capsys.readouterr().out)["error"]
        assert err["code"] == "routed_target_needs_contract" and NOTE in err["message"]

    def test_the_gate_decision_comes_before_the_gate(self):
        """Source discipline: the routed check sits ahead of the live validation, in the one
        place both `target add` and `onboard` run it."""
        import inspect
        src = inspect.getsource(ascend.cmd_onboard)
        gate = src[src.index("# 2. hard gate"):src.index("# 3. register")]
        assert gate.index("_routed_by_straiker(") < gate.index("V.validate_config(")


class _Console:
    """The platform's own records, which inspect may still consult: nothing registered."""
    @staticmethod
    def _rows(payload):
        return payload["data"]

    def list_apps(self):
        return {"data": []}


class TestTargetInspect:
    @pytest.fixture(autouse=True)
    def wired(self, monkeypatch):
        monkeypatch.delenv("ASCEND_TARGET_AUTH_FILE", raising=False)
        monkeypatch.setattr(profiles, "detect", never_reached)     # one GET per profile — not here
        monkeypatch.setattr(ascend, "_client", lambda args: _Console())

    def _args(self, url, **kw):
        return types.SimpleNamespace(**{"source": url, "insecure": False, "header": None,
                                        "bearer": None, "json": False, **kw})

    @pytest.mark.parametrize("url", ROUTED)
    def test_it_reports_the_note_without_touching_the_target(self, url, capsys):
        ascend.cmd_target_inspect(self._args(url))
        out = capsys.readouterr().out
        assert "transport   api" in out and NOTE in out
        assert "needs a bridge" not in out and "relay" not in out

    @pytest.mark.parametrize("url", ROUTED)
    def test_the_json_says_routed_and_direct(self, url, capsys):
        ascend.cmd_target_inspect(self._args(url, json=True))
        out = json.loads(capsys.readouterr().out)
        assert out["routed_by_straiker"] is True and out["reachable_from_cloud"] is True
        assert out["transport"] == "api" and NOTE in out["note"]
        assert out["already_registered"] == []


class TestTargetCheck:
    @pytest.fixture(autouse=True)
    def wired(self, monkeypatch):
        monkeypatch.setattr(V, "validate_config", never_reached)
        monkeypatch.setattr(creds, "load_all", lambda: {})
        monkeypatch.setattr(creds, "get", lambda app_id: None)

    def _cfg(self, tmp_path, url):
        p = tmp_path / "routed.json"
        p.write_text(json.dumps({"adapter": "direct_api", "endpoint": url, "method": "POST",
                                 "body": {"message": "{{PROMPT}}"}, "response_path": "message"}))
        return p

    @pytest.mark.parametrize("url", ROUTED)
    def test_it_says_the_note_and_exits_clean_without_claiming_a_pass(self, url, tmp_path, capsys):
        args = types.SimpleNamespace(target=str(self._cfg(tmp_path, url)), config=None, file=None,
                                     adapter=None, prompt="hi", expect=None, timeout=5.0, json=True)
        with pytest.raises(SystemExit) as ei:
            ascend.cmd_target_check(args)
        assert ei.value.code == ascend.EXIT_OK
        out = json.loads(capsys.readouterr().out)
        assert out["routed"] is True and out["checked"] is False and out["ok"] is None
        assert out["note"] == NOTE and out["host"] == url.split("/")[2]

    @pytest.mark.parametrize("url", ROUTED)
    def test_the_human_line_is_the_note(self, url, tmp_path, capsys):
        args = types.SimpleNamespace(target=str(self._cfg(tmp_path, url)), config=None, file=None,
                                     adapter=None, prompt="hi", expect=None, timeout=5.0, json=False)
        with pytest.raises(SystemExit) as ei:
            ascend.cmd_target_check(args)
        assert ei.value.code == ascend.EXIT_OK
        out = capsys.readouterr().out
        assert NOTE in out and "unreachable" not in out and "resolve" not in out

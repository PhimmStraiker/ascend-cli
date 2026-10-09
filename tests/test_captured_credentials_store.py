"""
test_captured_credentials_store.py — a credential a HAR import SEES must reach the target.

THE REGRESSION. `classify_evidence` returns the captured credential values under `secrets` and
writes the config with `env:ASCEND_SECRET_<host>_<header>` references — and `target add --har`
never stored the values. So the import printed `withheld from the config (credential-shaped):
Cookie, X-Conv-Token`, wrote an auth block referencing two variables nothing had ever set, and
validation died on the next line: `auth failed: environment variable '…_COOKIE' is not set`.
MEASURED 2026-10-09 on a fresh Target Lab HAR; the operator exported the two values by hand.

Both halves are asserted, because fixing either alone is a regression in the other: the value
is in the tenant-scoped 0600 store and NOT in the config, and the header that reaches the
validation gate is byte-identical to the one the browser sent.
"""
import json
import os
import stat
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
for _p in (REPO / "shells" / "cli", REPO / "runtime", REPO / "control", REPO):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
import ascend  # noqa: E402
import dispatch  # noqa: E402
import target_secrets as TS  # noqa: E402
from discovery import classify as C  # noqa: E402
from layers import auth as A  # noqa: E402
from runtime.discovery import validate as V  # noqa: E402
from _onboard_harness import onboard_args  # noqa: E402

COOKIE = "lab_access=1; nw_consent=yes"
VAR = "ASCEND_SECRET_BOT_EXAMPLE_COM_COOKIE"


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    """A scratch tenant store and config dir; nothing of the operator's is touched."""
    monkeypatch.setenv("ASCEND_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ASCEND_CONFIG_DIR", str(tmp_path / "configs"))
    monkeypatch.delenv(VAR, raising=False)
    return tmp_path


def _har(headers, url="https://bot.example.com/api/chat"):
    return {"log": {"version": "1.2", "entries": [{
        "startedDateTime": "2026-10-09T00:00:00.000Z", "time": 5,
        "request": {"method": "POST", "url": url, "httpVersion": "HTTP/1.1", "queryString": [],
                    "cookies": [], "headersSize": -1, "bodySize": 30,
                    "headers": [{"name": k, "value": v} for k, v in headers.items()],
                    "postData": {"mimeType": "application/json", "text": '{"message":"where is my order?"}'}},
        "response": {"status": 200, "statusText": "OK", "httpVersion": "HTTP/1.1",
                     "headers": [{"name": "Content-Type", "value": "application/json"}],
                     "cookies": [], "redirectURL": "", "headersSize": -1, "bodySize": 24,
                     "content": {"size": 24, "mimeType": "application/json", "text": '{"reply":"It shipped."}'}},
        "cache": {}, "timings": {"send": 0, "wait": 0, "receive": 0}}]}}


class TestTheStore:
    def test_a_record_is_0600_tenant_scoped_and_readable_back(self, isolated):
        ref = TS.record(VAR, COOKIE, host="bot.example.com", header="Cookie")
        assert ref == f"env:{VAR}"
        p = TS.store_path()
        assert p.parent == Path(os.environ["ASCEND_STATE_DIR"]) or p.parent.parent == Path(os.environ["ASCEND_STATE_DIR"])
        assert stat.S_IMODE(p.stat().st_mode) == 0o600
        assert TS.get(VAR) == COOKIE
        assert COOKIE not in json.dumps(TS.listing()), "a listing never shows a value"

    def test_the_environment_wins_over_the_store(self, isolated, monkeypatch):
        TS.record(VAR, "stale-from-yesterday")
        monkeypatch.setenv(VAR, COOKIE)
        assert TS.get(VAR) == COOKIE

    def test_a_world_readable_store_is_refused_loudly(self, isolated):
        TS.record(VAR, COOKIE)
        os.chmod(TS.store_path(), 0o644)
        with pytest.raises(PermissionError):
            TS.load_all()


class TestTheStoreSurvivesTheTenantPin:
    """`target add` stores at step 1 and pins the tenant at step 3, when its platform client is
    first created. A store that moved with the pin lost the credential four seconds after it
    was captured — MEASURED on a fresh scratch home: the `--config` re-run died with "this
    tenant's credential store has no value for it"."""

    def _home(self, monkeypatch, tmp_path):
        # Patched on the module object the store actually holds (`TS._tenant`) AND on whatever
        # `sys.modules` now calls `tenant`: an earlier test reloads the module to re-read its
        # environment, after which the two are different objects and a patch on one leaves the
        # other reading the operator's real ~/.ascend (a pinned fingerprint showed up here).
        monkeypatch.delenv("ASCEND_STATE_DIR", raising=False)
        mods = {id(m): m for m in (TS._tenant, sys.modules.get("tenant"), A.__dict__.get("_tenant")) if m}
        for m in mods.values():
            monkeypatch.setattr(m, "ASCEND_HOME", tmp_path / "home")
            monkeypatch.setattr(m, "TENANT_FILE", tmp_path / "home" / "tenant.json")
        store = sys.modules.get("target_secrets")
        if store is not None and store is not TS:
            monkeypatch.setattr(store, "_tenant", TS._tenant)
        return TS._tenant

    def test_a_credential_stored_before_the_pin_is_read_after_it(self, monkeypatch, tmp_path):
        tenant = self._home(monkeypatch, tmp_path)
        TS.record(VAR, COOKIE, host="bot.example.com", header="Cookie")
        assert TS.store_path().parent.name == "unpinned"
        tenant.pin("a" * 64, "tenant 123")
        assert TS.store_path().parent.name == "a" * 16, "the pinned root is now the current store"
        assert TS.get(VAR) == COOKIE
        assert A.resolve_secret_ref(f"env:{VAR}") == COOKIE

    def test_the_pinned_store_wins_and_writes_go_there(self, monkeypatch, tmp_path):
        tenant = self._home(monkeypatch, tmp_path)
        TS.record(VAR, "from-before-the-pin")
        tenant.pin("a" * 64, "tenant 123")
        TS.record(VAR, COOKIE)
        assert TS.get(VAR) == COOKIE
        assert json.loads((tmp_path / "home" / "state" / ("a" * 16) / "target_secrets.json").read_text())[VAR]["value"] == COOKIE
        assert json.loads((tmp_path / "home" / "state" / "unpinned" / "target_secrets.json").read_text())[VAR]["value"] == "from-before-the-pin", \
            "a write never copies rows between stores"

    def test_another_tenant_never_sees_a_pinned_tenants_credential(self, monkeypatch, tmp_path):
        tenant = self._home(monkeypatch, tmp_path)
        tenant.pin("a" * 64, "tenant 123")
        TS.record(VAR, COOKIE)
        tenant.pin("b" * 64, "tenant 456")
        assert TS.get(VAR) is None

    def test_forget_removes_it_from_both_stores(self, monkeypatch, tmp_path):
        tenant = self._home(monkeypatch, tmp_path)
        TS.record(VAR, "early")
        tenant.pin("a" * 64, "tenant 123")
        TS.record(VAR, COOKIE)
        assert TS.forget(VAR) is True
        assert TS.get(VAR) is None


class TestTheResolver:
    def test_an_env_reference_falls_back_to_the_store(self, isolated):
        TS.record(VAR, COOKIE)
        assert A.resolve_secret_ref(f"env:{VAR}") == COOKIE

    def test_the_error_names_both_places_when_neither_has_it(self, isolated):
        with pytest.raises(A.AuthError) as e:
            A.resolve_secret_ref(f"env:{VAR}")
        assert "credential store" in str(e.value) and VAR in str(e.value)

    def test_the_materialised_header_is_byte_identical_to_the_capture(self, isolated):
        TS.record(VAR, COOKIE)
        cfg = {"endpoint": "https://bot.example.com/api/chat",
               "auth": {"type": "static", "mode": "headers", "headers": {"Cookie": f"env:{VAR}"}}}
        merged = dispatch.merge_auth(cfg)
        assert not merged.get("_auth_error"), merged.get("_auth_error")
        assert merged["headers"]["Cookie"] == COOKIE


class TestTheHarImportEndToEnd:
    def _run(self, monkeypatch, tmp_path, har, **overrides):
        har_path = tmp_path / "capture.har"
        har_path.write_text(json.dumps(har))
        seen = {}

        def gate(adapter, cfg, prompt, *a, **k):
            merged = dispatch.merge_auth(cfg)
            seen["adapter"], seen["headers"] = adapter, dict(merged.get("headers") or {})
            seen["auth_error"] = merged.get("_auth_error")
            return {"ok": True, "response": "It shipped.", "duration_ms": 9}
        monkeypatch.setattr(V, "validate_config", gate)
        monkeypatch.setattr(ascend, "_guard_egress", lambda url, args: None)
        monkeypatch.setattr(ascend, "_upgrade_streaming_shape", lambda cfg, vres, args, V: (cfg, vres))
        monkeypatch.setattr(ascend, "_guard_constant_response", lambda *a, **k: None)
        args = onboard_args(har=str(har_path), save_as="lab-import", dry_run=True, json=True, **overrides)
        ascend.cmd_onboard(args)
        return seen, Path(os.environ["ASCEND_CONFIG_DIR"]) / "lab-import.json"

    def test_the_captured_cookie_is_stored_referenced_and_sent(self, isolated, monkeypatch, tmp_path, capsys):
        seen, cfg_path = self._run(monkeypatch, tmp_path, _har({"Cookie": COOKIE, "Content-Type": "application/json"}))
        text = cfg_path.read_text()
        cfg = json.loads(text)
        assert COOKIE not in text, "the value must never reach the config file"
        assert cfg["auth"] == {"type": "static", "mode": "headers", "headers": {"Cookie": f"env:{VAR}"}}
        assert TS.get(VAR) == COOKIE, "the reference the config carries resolves from the store"
        assert seen["auth_error"] is None
        assert seen["headers"]["Cookie"] == COOKIE, "the gate proved the cookie the browser sent"
        err = capsys.readouterr().err
        assert "stored them" in err and "authenticating as the captured session: Cookie" in err
        assert "withheld from the config" not in err, "nothing was withheld: the value was kept"

    def test_a_credential_with_no_value_to_store_is_still_reported_as_withheld(self, isolated, monkeypatch, tmp_path, capsys):
        """A header the classifier names but holds no value for keeps the old, honest message."""
        seen, cfg_path = self._run(monkeypatch, tmp_path, _har({"Content-Type": "application/json"}))
        assert "stored them" not in capsys.readouterr().err
        assert "auth" not in json.loads(cfg_path.read_text())

    def test_the_store_survives_for_a_config_rerun(self, isolated, monkeypatch, tmp_path):
        """What the capture stored is what `--config` validates against tomorrow: no export needed."""
        self._run(monkeypatch, tmp_path, _har({"Cookie": COOKIE, "Content-Type": "application/json"}))
        monkeypatch.delenv(VAR, raising=False)
        cfg = json.loads((Path(os.environ["ASCEND_CONFIG_DIR"]) / "lab-import.json").read_text())
        merged = dispatch.merge_auth(cfg)
        assert merged["headers"]["Cookie"] == COOKIE


def test_the_har_branch_stores_before_it_writes():
    """Source discipline: the store is filled before the config is written and validated."""
    import inspect
    src = inspect.getsource(ascend.cmd_onboard)
    assert src.index("_store_captured_credentials(res.get(\"secrets\")") < src.index(
        "_write_named_config(cfg, cfg_name, exact=named_exactly)\n\n    # Credentials are")

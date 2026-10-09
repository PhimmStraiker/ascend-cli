"""
_onboard_harness — drive `target add` (cmd_onboard) offline, from a saved config to registration.

Not a test module: the leading underscore keeps pytest from collecting it. It wires the seams
cmd_onboard crosses — config resolution, the live hard gate, the platform client, the config
binding — to fakes, and records what reached the platform. `--config <name>` is the shortest route
into step 3 (register): a saved config derives nothing and probes nothing, so the only things left
on the path are the gate and the registration, which is what these tests are about.

No socket is ever opened: a resolver call fails the test outright rather than being mocked, so a
test that passes here passes on a machine with no network.
"""
from __future__ import annotations

import json
import socket
import sys
import types
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
for _p in (REPO / "shells" / "cli", REPO / "runtime", REPO / "control", REPO):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
import ascend  # noqa: E402
from runtime.discovery import validate as V  # noqa: E402


def no_lookup(*a, **k):
    raise AssertionError(f"a DNS lookup was attempted: getaddrinfo{a}")


def never_reached(*a, **k):
    raise AssertionError("the live hard gate was reached; it must not run for this target")


# What the engine's gate answers for a generated adaptor: the template value is what lands on the
# app. "FRESH" is literal on purpose, so a test can tell a re-generated adaptor from a stored one.
GATE_OK = {"ok": True, "origin": "inline", "templateKey": "_adaptor_src", "templateValue": "FRESH-ADAPTOR",
           "digest": "0094886620bc", "sizes": {"bytes": 4000, "minifiedBytes": 1500, "encodedBytes": 2000},
           "gate": {"ok": True, "violations": [], "caps": ["http.request"], "entries": ["sendTurn"]}}
TURN_OK = {"n": 1, "status_code": 200, "ms": 612, "scored": "Hello! I can help with orders.",
           "response": "Hello! I can help with orders.", "body": {"reply": "Hello! I can help with orders."}}
CONSOLE_UUID = "01a1220c-0d0f-722a-a94b-59bb8a3c6276"


class FakePlatform:
    """The slice of the platform client `cmd_onboard` talks to, recording every write — and, for
    the adaptor default, the engine's routes (gate, test, verify) and the Console join."""

    def __init__(self, existing=None, *, gate=None, test=None, verify=None, console_uuid=CONSOLE_UUID):
        self.existing = existing            # the app already registered under this name, or None
        self.patches, self.created = [], []
        self.gate_out = GATE_OK if gate is None else gate
        self.test_out = {"turns": [TURN_OK]} if test is None else test
        self.verify_out = {"ok": True, "turns": [TURN_OK], "preflight": None} if verify is None else verify
        self.console_uuid, self.last_console_error = console_uuid, None
        self.gated, self.tested, self.verified, self.console_asked = [], [], [], []
        self._records = {}                  # app id -> the record as the platform now holds it

    def find_app_by_name(self, name):
        return self.existing

    def get_app(self, app_id):
        if app_id in self._records:
            return dict(self._records[app_id])
        return dict(self.existing or {})

    def patch_app(self, app_id, patch):
        self.patches.append((app_id, json.loads(json.dumps(patch))))
        base = self._records.get(app_id) or dict(self.existing or {})
        self._records[app_id] = {**base, **json.loads(json.dumps(patch))}
        return {}

    def create_app(self, spec):
        self.created.append(json.loads(json.dumps(spec)))
        app = {"id": "aapp_new", "name": spec.get("name"), "api_type": spec.get("api_type"),
               "thin_api_key": "tc-new" if spec.get("api_type") == "thin" else None,
               **{k: spec[k] for k in ("url", "request_template", "response_template", "headers") if k in spec}}
        self._records[app["id"]] = app
        return dict(app)

    # --- the engine's adaptor routes and the Console join ---
    def adapter_gate(self, source):
        self.gated.append(source)
        return self.gate_out

    def adapter_test(self, source, app_id, prompts, budget):
        self.tested.append((source, app_id, list(prompts), budget))
        return self.test_out

    def verify_app_adapter(self, app_id, budget):
        self.verified.append((app_id, budget))
        return self.verify_out

    def console_app_uuid(self, name, *, url=None, attempts=1, delay_s=0):
        self.console_asked.append((name, url))
        return self.console_uuid

    def validate_controls(self, ids):
        return {"valid": list(ids), "warnings": [], "unknown": []}

    def list_apps(self):
        return {"data": [self.existing] if self.existing else []}

    def list_controls(self):
        return {"controls": [{"id": "ctl_a"}, {"id": "ctl_b"}]}


def onboard_args(**overrides):
    """Every attribute cmd_onboard reads, at the value `target add` would give it by default."""
    base = dict(
        name=None, har=None, curl=None, api=None, ws=None, url=None, config=None, module=None,
        scaffold=None, spec=None, save_as=None, prompt="hello", prompt_hint=None, headless=True,
        settle=5, manual=False, cdp=None, save_evidence=None, warmup=None, adapter=None,
        timeout=10.0, dry_run=False, controls=None, force=False, app=None, via="auto",
        system_prompt=None, size=None, qpm=None, purpose=None, stop_after_register=True,
        json=True, verbose=False, insecure=False, no_profile=False, workspace=None, bearer=None,
        header=None, api_key=None, assessment_name=None, wait=False, interval=5,
        timeout_assess=0, detail=False, run=False, allow_internal=False, timeout_ms=None,
    )
    base.update(overrides)
    return types.SimpleNamespace(**base)


def run_target_add(monkeypatch, tmp_path, cfg, platform, *, gate="answers", **arg_overrides):
    """`ascend target add --config mybot --json` against `platform`, with the live gate faked.

    gate="answers": the target is taken to have replied. gate="poison": reaching the gate fails
    the test — for a target that must never be validated from this machine.
    """
    path = tmp_path / "mybot.json"
    path.write_text(json.dumps(cfg))
    monkeypatch.setattr(ascend, "resolve_config_path",
                        lambda name: path if str(name).startswith("mybot") else None)
    monkeypatch.setattr(ascend, "_load_named_config", lambda name: json.loads(path.read_text()))
    monkeypatch.setattr(ascend, "_client", lambda args: platform)
    monkeypatch.setattr(ascend, "_bind_config", lambda *a, **k: True)
    # the generated adaptor lands beside the config: keep it inside tmp_path
    monkeypatch.setattr(ascend, "_write_adaptor_file",
                        lambda cfg_path, cfg_name, source: (tmp_path / f"{cfg_name}.adaptor.js").write_text(source)
                        and (tmp_path / f"{cfg_name}.adaptor.js"))
    monkeypatch.setattr(ascend, "_guard_egress", lambda url, args: None)
    monkeypatch.setattr(ascend, "_upgrade_streaming_shape", lambda cfg, vres, args, V: (cfg, vres))
    monkeypatch.setattr(ascend, "_guard_constant_response", lambda *a, **k: None)
    monkeypatch.setattr(socket, "getaddrinfo", no_lookup)
    if gate == "answers":
        monkeypatch.setattr(V, "validate_config",
                            lambda *a, **k: {"ok": True, "response": "hi there", "duration_ms": 12})
    else:
        monkeypatch.setattr(V, "validate_config", never_reached)
    args = onboard_args(config="mybot", **arg_overrides)
    ascend.cmd_onboard(args)
    return args

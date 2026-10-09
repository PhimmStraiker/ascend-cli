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


class FakePlatform:
    """The slice of the platform client `cmd_onboard` talks to, recording every write."""

    def __init__(self, existing=None):
        self.existing = existing            # the app already registered under this name, or None
        self.patches, self.created = [], []

    def find_app_by_name(self, name):
        return self.existing

    def get_app(self, app_id):
        return dict(self.existing or {})

    def patch_app(self, app_id, patch):
        self.patches.append((app_id, json.loads(json.dumps(patch))))
        return {}

    def create_app(self, spec):
        self.created.append(json.loads(json.dumps(spec)))
        return {"id": "aapp_new", "name": spec.get("name"), "api_type": spec.get("api_type"),
                "thin_api_key": "tc-new" if spec.get("api_type") == "thin" else None}

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

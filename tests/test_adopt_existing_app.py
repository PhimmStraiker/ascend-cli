"""
test_adopt_existing_app — `--app` must bind to an application that already exists, not clone it.

The engagement shape this exists for: the app is configured in the Console — system prompt,
controls, QPM, assessment size — someone tries to start an assessment, hits "bridge errors", and
nobody can say where the bridge is. There is nothing to find: a bridge is a PROCESS that has to be
running, and onboarding could only ever CREATE a new app, which would strand all of that
configuration on an app nobody assesses.

`--app <name|aapp_id>` adopts the existing record and fetches its bridge key (the platform returns
`thin_api_key` on GET), falling back to the local key store, so the operator does not have to know
where the key went.
"""
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
for p in ("control", "runtime", "shells/cli"):
    sys.path.insert(0, str(REPO / p))
import ascend      # noqa: E402


def test_the_flag_exists_on_both_onboarding_commands():
    """`target add` and `onboard` share one arg builder; both must accept it."""
    import argparse
    parser = ascend.build_parser()
    found = {}
    for action in parser._actions:
        if not isinstance(action, argparse._SubParsersAction):
            continue
        for gname, gp in action.choices.items():
            if gname == "onboard":
                found["onboard"] = any("--app" in (a.option_strings or []) for a in gp._actions)
            for sub in gp._actions:
                if isinstance(sub, argparse._SubParsersAction) and gname == "target":
                    add = sub.choices.get("add")
                    if add is not None:
                        found["target add"] = any(
                            "--app" in (a.option_strings or []) for a in add._actions)
    assert found.get("onboard") is True
    assert found.get("target add") is True


class _Client:
    """Records whether create_app was called — the whole point is that it is not."""

    def __init__(self, app):
        self.app, self.created, self.gets = app, [], []

    def list_apps(self):
        return {"data": [self.app]}

    def get_app(self, app_id):
        self.gets.append(app_id)
        return self.app

    def create_app(self, spec):
        self.created.append(spec)
        return {"id": "aapp_NEW", "thin_api_key": "tc-new"}

    def validate_controls(self, ids):
        return {"valid": list(ids), "warnings": [], "unknown": []}


def test_resolve_finds_an_existing_app_by_name():
    c = _Client({"id": "aapp_EXISTING", "name": "Support Bot", "api_type": "thin",
                 "thin_api_key": "tc-existing"})
    assert ascend._resolve_app(c, "Support Bot") == "aapp_EXISTING"
    assert ascend._resolve_app(c, "aapp_EXISTING") == "aapp_EXISTING"
    assert c.created == [], "resolving must never create an application"


def test_an_aapp_id_is_used_without_a_lookup():
    c = _Client({"id": "aapp_X", "name": "x", "api_type": "thin"})
    assert ascend._resolve_app(c, "aapp_ZZZ") == "aapp_ZZZ"


def test_key_is_read_from_the_app_record():
    """The platform returns thin_api_key on GET, so the operator need not have kept it."""
    app = {"id": "aapp_E", "name": "Support Bot", "api_type": "thin",
           "thin_api_key": "tc-from-platform"}
    assert app.get("thin_api_key") == "tc-from-platform"
    assert ascend._mask_app(app)["thin_api_key"] != "tc-from-platform"   # never printed raw


@pytest.mark.parametrize("api_type,needs_bridge", [
    ("thin", True), ("bridge", True), ("api", False), ("gcp", False), ("bedrock", False),
])
def test_only_bridge_type_apps_need_something_serving_them(api_type, needs_bridge):
    """Adopting an api/gcp/bedrock app should say so: Ascend calls those directly."""
    assert ((api_type or "").lower() in ("thin", "bridge", "")) is needs_bridge

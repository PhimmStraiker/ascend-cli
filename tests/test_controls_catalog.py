"""
The control catalog: what the platform sends, and what the CLI must not lose or wave through.

`/ascend/controls` returns `{object, controls, categories}` — two lists. The categories carry the
platform's own risk tag (Security / Safety / Trust), display names, and membership. Controls carry
`deprecated` and `agentic` flags.

What these tests protect:
  - `controls validate` EXITS NON-ZERO on an unknown id. It used to print a warning and exit 0,
    so a typo'd control passed the check, generated zero probes, and produced a run that looked
    clean because it tested nothing.
  - exactly one JSON object on stdout, success or failure
  - the categories half of the payload stays reachable
  - deprecated controls are hidden by default (they generate zero probes) but countable
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "control"))
sys.path.insert(0, str(REPO))

import api  # noqa: E402

# A catalog shaped exactly like the live payload, with the flags that matter.
CATALOG = {
    "object": "list",
    "controls": [
        {"id": "phone_number", "name": "Phone Number", "prefix": "PII",
         "category_id": "data_leak"},
        {"id": "sys_prompt_leak", "name": "System Prompt Leak", "category_id": "sys_prompt_leak"},
        {"id": "agentic_rce", "name": "Agentic RCE", "category_id": "agent_vulnerabilities",
         "agentic": True},
        {"id": "tool_misuse", "name": "Tool Misuse", "category_id": "agent_vulnerabilities",
         "agentic": True, "deprecated": True},
        {"id": "prompt_injection", "name": "Prompt Injection", "category_id": "llm_evasion",
         "deprecated": True},
        {"id": "excessive_agency", "name": "Excessive Agency", "category_id": "excessive_agency",
         "deprecated": True},
    ],
    "categories": [
        {"id": "data_leak", "name": "Data Leakage", "tag": "Security",
         "control_ids": ["phone_number"]},
        {"id": "sys_prompt_leak", "name": "System Prompt Leak", "tag": "Security",
         "control_ids": ["sys_prompt_leak"]},
        {"id": "agent_vulnerabilities", "name": "Agentic Risks", "tag": "Security",
         "control_ids": ["agentic_rce", "tool_misuse"]},
        {"id": "llm_evasion", "name": "LLM Evasion", "tag": "Security",
         "control_ids": ["prompt_injection"]},
        {"id": "excessive_agency", "name": "Excessive Agency", "tag": "Security",
         "control_ids": ["excessive_agency"]},
        {"id": "app_grounding", "name": "Application Grounding", "tag": "Trust",
         "control_ids": []},
    ],
}


class TestValidateControls:
    def _v(self, ids):
        c = api.AscendAPI(token="s6r_pat_test")
        with mock.patch.object(api.AscendAPI, "list_controls", return_value=CATALOG):
            return c.validate_controls(ids)

    def test_a_good_id_is_valid(self):
        v = self._v(["phone_number"])
        assert v["valid"] == ["phone_number"]
        assert not v["unknown"] and not v["deprecated"]

    def test_unknown_id_is_separated_not_passed_through(self):
        v = self._v(["nope"])
        assert v["unknown"] == ["nope"]
        assert v["valid"] == []

    def test_deprecated_id_is_not_valid(self):
        """A deprecated control generates zero probes, so treating it as valid means the run
        silently measures less than the operator asked for."""
        v = self._v(["prompt_injection"])
        assert v["deprecated"] == ["prompt_injection"]
        assert "prompt_injection" not in v["valid"]

    def test_agentic_ids_are_flagged_but_still_valid(self):
        v = self._v(["agentic_rce"])
        assert v["agentic"] == ["agentic_rce"]
        assert v["valid"] == ["agentic_rce"]

    def test_a_bare_list_payload_does_not_crash(self):
        """Envelope drift must not become an AttributeError on `.get`."""
        c = api.AscendAPI(token="s6r_pat_test")
        with mock.patch.object(api.AscendAPI, "list_controls",
                               return_value=CATALOG["controls"]):
            v = c.validate_controls(["phone_number"])
        assert v["valid"] == ["phone_number"]


class TestCategoriesArePreserved:
    def test_the_payload_carries_both_lists(self):
        assert CATALOG["controls"] and CATALOG["categories"]

    def test_every_category_has_a_risk_tag(self):
        """Rollups group by the platform's tag, so a missing one silently drops a whole bucket."""
        assert all(g.get("tag") for g in CATALOG["categories"])
        assert {g["tag"] for g in CATALOG["categories"]} <= {"Security", "Safety", "Trust"}

    def test_a_category_whose_controls_are_all_deprecated_has_no_active_ones(self):
        dep = {c["id"] for c in CATALOG["controls"] if c.get("deprecated")}
        ea = next(g for g in CATALOG["categories"] if g["id"] == "excessive_agency")
        assert set(ea["control_ids"]) <= dep, \
            "selecting this category yields zero probes — the list view must say so"


# ---------------------------------------------------------------------------------------------
# the commands (subprocess, so exit codes are real)
# ---------------------------------------------------------------------------------------------

CLI = str(REPO / "shells" / "cli" / "ascend.py")

# The catalog is interpolated as a PYTHON literal (repr), not JSON: JSON's `true` is not a
# Python name, so json.dumps here would make the shim raise NameError inside the CLI.
CONFTEST_STUB = '''
import sys
sys.path.insert(0, r"{repo}/control")
import api
_CATALOG = {catalog}
api.AscendAPI.list_controls = lambda self: _CATALOG
api.AscendAPI._bearer = lambda self: "jwt"
'''


@pytest.fixture()
def stub_catalog(tmp_path, monkeypatch):
    """Run the CLI against a fixed catalog by pre-importing a patch via PYTHONSTARTUP-style shim."""
    shim = tmp_path / "sitecustomize.py"
    shim.write_text(CONFTEST_STUB.format(repo=REPO, catalog=repr(CATALOG)))
    env = {"PYTHONPATH": str(tmp_path), "STRAIKER_PAT": "s6r_pat_test",
           "ASCEND_STATE_DIR": str(tmp_path / "state"), "NO_COLOR": "1",
           "ASCEND_NO_SPINNER": "1", "PATH": "/usr/bin:/bin"}
    return env


def _run(env, *args):
    return subprocess.run([sys.executable, CLI, *args], capture_output=True, text=True,
                          cwd=str(REPO), env=env)


class TestValidateExitCodes:
    """The whole point of this command is its exit code."""

    def test_unknown_id_exits_nonzero(self, stub_catalog):
        r = _run(stub_catalog, "controls", "validate", "definitely_not_a_control")
        assert r.returncode != 0, \
            "an id that does not exist generates zero probes — passing it is a false clean run"
        assert "do not exist" in (r.stdout + r.stderr)

    def test_good_id_exits_zero(self, stub_catalog):
        r = _run(stub_catalog, "controls", "validate", "phone_number")
        assert r.returncode == 0, r.stderr

    def test_deprecated_warns_but_passes_by_default(self, stub_catalog):
        r = _run(stub_catalog, "controls", "validate", "prompt_injection")
        assert r.returncode == 0
        assert "deprecated" in (r.stdout + r.stderr).lower()

    def test_deprecated_fails_under_strict(self, stub_catalog):
        r = _run(stub_catalog, "controls", "validate", "prompt_injection", "--strict")
        assert r.returncode != 0

    def test_exactly_one_json_object_on_failure(self, stub_catalog):
        """Emitting the success envelope and then the error envelope is unparseable."""
        r = _run(stub_catalog, "controls", "validate", "nope", "--json")
        payload = json.loads(r.stdout)      # raises if two objects were printed
        assert payload["ok"] is False
        assert payload["error"]["code"] == "unknown_control"

    def test_exactly_one_json_object_on_success(self, stub_catalog):
        r = _run(stub_catalog, "controls", "validate", "phone_number", "--json")
        payload = json.loads(r.stdout)
        assert payload["ok"] is True
        assert payload["valid"] == ["phone_number"]


class TestListViews:
    def test_deprecated_hidden_by_default_and_countable(self, stub_catalog):
        plain = _run(stub_catalog, "controls", "list")
        allc = _run(stub_catalog, "controls", "list", "--include-deprecated")
        assert plain.returncode == 0 and allc.returncode == 0, plain.stderr + allc.stderr
        assert "total=3" in plain.stdout, plain.stdout
        assert "total=6" in allc.stdout, allc.stdout

    def test_categories_view_shows_tag_and_active_counts(self, stub_catalog):
        r = _run(stub_catalog, "controls", "list", "--categories")
        assert r.returncode == 0, r.stderr
        assert "Security" in r.stdout and "Trust" in r.stdout
        assert "Data Leakage" in r.stdout
        # a category whose controls are all deprecated must be called out, not shown as normal
        assert "no active controls" in r.stdout

    def test_tag_filter_uses_the_platform_grouping(self, stub_catalog):
        r = _run(stub_catalog, "controls", "list", "--tag", "Trust")
        assert r.returncode == 0, r.stderr
        assert "total=0" in r.stdout, "no active control is tagged Trust in this catalog"

    def test_flags_render_only_when_set(self, stub_catalog):
        r = _run(stub_catalog, "controls", "list", "--include-deprecated")
        assert "[deprecated, agentic]" in r.stdout
        assert "[--]" not in r.stdout, "a placeholder flag on every row trains the eye to skip it"

    def test_agentic_only(self, stub_catalog):
        r = _run(stub_catalog, "controls", "list", "--agentic-only")
        assert "agentic_rce" in r.stdout
        assert "phone_number" not in r.stdout

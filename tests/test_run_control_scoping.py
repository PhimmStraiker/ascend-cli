"""
test_run_control_scoping.py — `assess run --controls` must narrow the run, not just the warnings.

`AscendAPI.create_assessment` accepts exactly one field, `{"name"}`; its own docstring records
that "the run inherits the app's controls/size/QPM". There is no per-run control override on the
platform. `cmd_assess_run` nevertheless took `--controls`, ran it through `validate_controls`,
printed the warnings, and then **dropped the result on the floor** — nothing carried it to the
create call, because `c.run()` has no parameter to carry it in.

So the flag narrowed nothing. Measured on the demo tenant: an app registered by `target add` with
no `--controls` carries all 62 non-deprecated catalog controls, and

    ascend assess run --app <t> --name check --controls sys_prompt_leak

produced **665 probes** — ~10.7 per control across the full registered set, not the ~11 the
operator asked for. Every one of those is a live request to the customer's agent, spending their
rate limit, and the only clue was a probe counter climbing past a number nobody expected.

That is worse than a flag that does not exist. A missing flag is discovered immediately; this one
reads as scoping, is spelled like scoping, and its old help text ("validate these ids first") is
technically accurate in a way no operator would ever parse as "and then runs everything anyway".

The fix writes the selection to the app before the run is created, which is the only place the
run reads it from. That is a persistent change and it is announced as one.

The tests below cover the three ways this can regress:
  1. the helper does the wrong thing,
  2. the helper is right but a call site does not use it (the drift that caused four separate
     bugs this release — a unit test on the helper alone passes against that),
  3. the flag stops being applied at all and quietly returns to validate-and-discard.
"""
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "shells" / "cli"))
sys.path.insert(0, str(REPO / "runtime"))
sys.path.insert(0, str(REPO / "control"))
import ascend  # noqa: E402

SRC = (REPO / "shells" / "cli" / "ascend.py").read_text()


class _Args:
    json = False
    verbose = False


class _Client:
    """Records the PATCH so the test can assert the payload shape the platform requires."""

    def __init__(self, existing=None, fail=False):
        self._existing = existing
        self._fail = fail
        self.patches = []

    def get_app(self, app_id):
        return {"control_ids": self._existing} if self._existing is not None else {}

    def patch_app(self, app_id, patch):
        if self._fail:
            raise RuntimeError("upstream refused")
        self.patches.append((app_id, patch))
        return {"ok": True}


class TestTheSelectionReachesTheApp:
    def test_it_patches_the_app_with_the_requested_ids(self):
        c = _Client(existing=["a", "b", "c"])
        note = ascend._scope_run_controls(c, "aapp_x", ["sys_prompt_leak"], _Args())
        assert c.patches, "nothing was written to the app — the run would use the full set"
        _, patch = c.patches[0]
        assert patch["control_ids"] == ["sys_prompt_leak"]
        assert note and "1 control" in note

    def test_it_sends_the_only_shape_the_platform_accepts(self):
        """`control_type: "all"` is rejected 400; custom + explicit ids is the one accepted shape."""
        c = _Client(existing=["a"])
        ascend._scope_run_controls(c, "aapp_x", ["pii_leak"], _Args())
        assert c.patches[0][1]["control_type"] == "custom"

    def test_the_note_reports_what_the_set_was(self):
        c = _Client(existing=["a"] * 62)
        note = ascend._scope_run_controls(c, "aapp_x", ["sys_prompt_leak"], _Args())
        assert "was 62" in note, "the operator should see the size of the set being replaced"

    def test_no_controls_means_no_write(self):
        """A run without --controls must not touch the app's configuration."""
        c = _Client(existing=["a"])
        assert ascend._scope_run_controls(c, "aapp_x", None, _Args()) is None
        assert not c.patches

    def test_an_already_scoped_app_is_not_rewritten(self):
        c = _Client(existing=["sys_prompt_leak"])
        assert ascend._scope_run_controls(c, "aapp_x", ["sys_prompt_leak"], _Args()) is None
        assert not c.patches, "re-scoping to the same set is a wasted call and a confusing note"

    def test_a_failed_scope_refuses_rather_than_running_everything(self):
        """Falling through here is exactly the 665-probe surprise, with a warning nobody reads."""
        c = _Client(existing=["a"], fail=True)
        with pytest.raises(SystemExit):
            ascend._scope_run_controls(c, "aapp_x", ["sys_prompt_leak"], _Args())

    def test_an_unreadable_current_set_still_scopes(self):
        """Not knowing the previous set is no reason to skip narrowing the run."""
        class NoGet(_Client):
            def get_app(self, app_id):
                raise RuntimeError("nope")
        c = NoGet()
        assert ascend._scope_run_controls(c, "aapp_x", ["sys_prompt_leak"], _Args())
        assert c.patches


class TestBothRunPathsApplyIt:
    """The drift guard. The single-app path and the fleet path are two call sites of one rule."""

    def _body(self, fn_name):
        m = re.search(rf"^def {fn_name}\(.*?\):(.*?)(?=^def )", SRC, re.S | re.M)
        assert m, f"could not find {fn_name} in ascend.py"
        return m.group(1)

    @pytest.mark.parametrize("fn", ["cmd_assess_run", "_assess_run_many"])
    def test_the_run_path_scopes_before_creating(self, fn):
        assert "_scope_run_controls(" in self._body(fn), (
            f"{fn} starts an assessment without applying --controls to the app — the run will "
            f"use the app's full registered set and the flag will have narrowed nothing")

    def test_the_fleet_path_receives_the_validated_ids(self):
        """A fleet started with --controls fans the full catalog across every bound target."""
        assert "scope_ids=scope_ids" in self._body("cmd_assess_run"), (
            "the fleet path is called without the validated selection, so --all-bound "
            "--controls would scope nothing across the whole fleet")


class TestTheFlagNoLongerClaimsToOnlyValidate:
    def test_the_help_text_says_it_scopes(self):
        p = ascend.build_parser()
        assess = [a for a in p._actions if getattr(a, "choices", None)][0].choices["assess"]
        run = [a for a in assess._actions if getattr(a, "choices", None)][0].choices["run"]
        h = next(a.help for a in run._actions if "--controls" in (a.option_strings or []))
        assert "scope" in h.lower(), f"--controls help still describes validation only: {h!r}"


class TestTheApiStillHasNoPerRunOverride:
    """Documents WHY the app has to be patched, so nobody 'simplifies' this into the create call.

    If `create_assessment` ever grows a control parameter, scoping should move there — it would
    stop mutating the app, which is strictly better. This test is the breadcrumb.
    """

    def test_create_assessment_takes_only_a_name(self):
        api_src = (REPO / "control" / "api.py").read_text()
        m = re.search(r"def create_assessment\(self,([^)]*)\)", api_src)
        assert m, "create_assessment not found"
        params = [p.strip() for p in m.group(1).split(",") if p.strip()]
        assert params == ["app_id: str", "name: str"], (
            f"create_assessment signature changed to {params} — if it now accepts controls, "
            f"scope the run there instead of patching the app")

    def test_the_run_helper_still_cannot_carry_controls(self):
        api_src = (REPO / "control" / "api.py").read_text()
        m = re.search(r"def run\(self,(.*?)\) -> Any:", api_src, re.S)
        assert m and "control" not in m.group(1), (
            "AscendAPI.run now takes controls — pass them through instead of patching the app")

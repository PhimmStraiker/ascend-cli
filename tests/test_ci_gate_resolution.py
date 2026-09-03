"""
test_ci_gate_resolution.py — `ascend ci --app <name>` must gate something.

Two defects made the documented CI invocation unusable on every app it was pointed at. They
compound: fixing only the first turns a 404 into a refusal.

1. NO ASSESSMENT WAS RESOLVED.

   `cmd_ci` read `args.assessment` and passed it straight into the URL. With `--app` alone that
   value is None, so the request went to

       GET /ascend/applications/aapp_.../assessments/None -> 404 assessment_not_found

   on 5 of 5 apps in field testing. The advice printed next to it — "not found, check the app
   id/name with `ascend app list`" — named the one thing that HAD resolved correctly.

   `--app` alone can only mean "the latest finished run on this app", and a gate wants a FINISHED
   one: a run still in progress has partial counts, so gating on those either passes a build early
   or fails it for findings that have not arrived yet.

2. THE TRUST FLOOR WAS SMALLER THAN THE SMALLEST LEGITIMATE RUN.

   `MIN_CREDIBLE_PROBES = 5` guards a real failure: a completed run with almost no probes and no
   findings is what a dead bridge produces, and it scores clean having measured nothing. But the
   number was picked before anyone measured a scoped run. Measured here: one control at size
   `small` produces exactly FOUR probes. So the cheapest run the tool recommends could never pass
   its own gate, and the refusal blamed a bridge that had answered every probe:

       cannot trust results: the run completed with only 4 probe(s) and no findings. That is
       what a bridge that was not running produces... Check `ascend bridge ls`

   The floor is now derived from the control set — at least one probe per control the run was
   scoped to. Four probes for one control is a complete run; four probes for sixty-two controls is
   the dead bridge the check exists to catch. That distinction is the whole point, and an absolute
   constant cannot express it.
"""
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
for p in ("shells/cli", "runtime", "control", "."):
    sys.path.insert(0, str(REPO / p))
from reporting import ci as CI                       # noqa: E402

SRC = (REPO / "shells" / "cli" / "ascend.py").read_text()
API = (REPO / "control" / "api.py").read_text()


class TestTheFloorIsDerivedNotGuessed:
    def test_a_single_control_run_of_four_probes_is_credible(self):
        """The measured size of the cheapest recommended run. This is the regression."""
        assert CI.credible_probe_floor(1) <= 4, (
            "a one-control run produces 4 probes; a floor above that refuses the run the cost "
            "guidance tells you to start with, and blames the bridge for it")

    def test_a_full_catalog_run_of_four_probes_is_not_credible(self):
        """The failure the floor exists for must still be caught."""
        assert CI.credible_probe_floor(62) > 4

    @pytest.mark.parametrize("n", [1, 2, 5, 62, 71])
    def test_the_floor_never_exceeds_one_probe_per_control(self, n):
        assert CI.credible_probe_floor(n) <= n

    @pytest.mark.parametrize("bad", [None, "", "many", -1, 0])
    def test_an_unknown_control_count_falls_back_to_the_constant(self, bad):
        assert CI.credible_probe_floor(bad) == CI.MIN_CREDIBLE_PROBES

    def test_the_absolute_constant_still_exists_for_callers_without_an_app(self):
        assert isinstance(CI.MIN_CREDIBLE_PROBES, int) and CI.MIN_CREDIBLE_PROBES > 0


class TestTheGateStillRefusesADeadBridge:
    """Deriving the floor must not defeat the check it replaced."""

    # Captured verbatim from the live run this fix was verified against (asmt_1gG8Smpu...,
    # one control, four probes, clean). A hand-written assessment agrees with whatever the test
    # author assumed the shape was -- the first draft here omitted `category_summary` entirely and
    # the gate refused it as unreadable, which looked exactly like the bug under test.
    def _run(self, total, controls):
        cur = {
            "id": "asmt_x", "object": "ascend.assessment", "status": "complete",
            "name": "gate-fixture", "type": "scheduled", "score": 0, "progress": 1,
            "total": total, "severity": "low", "trend": 0, "summary": "…",
            "category_summary": [{
                "id": "sys_prompt_leak", "name": "System Prompt Leak", "description": "",
                "failed": 0, "total": total, "score": 0, "severity": "medium", "status": "pass",
                "controls": [{"id": "sys_prompt_leak", "failed": 0, "total": total,
                              "severity": "medium", "status": "pass"}],
            }],
        }
        return CI.gate(cur, None, fail_on_severity="high",
                       min_probes=CI.credible_probe_floor(controls))

    def test_a_full_catalog_run_that_produced_four_probes_is_refused(self):
        r = self._run(4, 62)
        assert r.get("exit_code") or r.get("untrusted") or not r.get("passed", True), (
            "62 controls that produced 4 probes is a dead bridge and must not gate green")

    def test_a_one_control_run_that_produced_four_probes_is_allowed(self):
        r = self._run(4, 1)
        assert r.get("exit_code", 0) == 0, f"a complete scoped run was refused: {r}"

    def test_a_run_that_produced_nothing_is_always_refused(self):
        assert self._run(0, 1).get("exit_code", 0) != 0


class TestTheLatestAssessmentIsResolved:
    class _C:
        def __init__(self, rows):
            self.rows = rows

        def _req(self, method, path, **kw):
            return self.rows

    def _latest(self, rows, **kw):
        import api
        c = api.AscendAPI.__new__(api.AscendAPI)
        c._req = lambda *a, **k: rows
        return api.AscendAPI.latest_assessment(c, "aapp_x", **kw)

    def test_the_newest_finished_run_wins(self):
        rows = [{"id": "old", "status": "complete", "created_at": "2026-01-01"},
                {"id": "new", "status": "complete", "created_at": "2026-06-01"}]
        assert self._latest(rows)["id"] == "new"

    def test_a_running_assessment_is_skipped_for_a_gate(self):
        """Partial counts either pass a build early or fail it for findings not yet in."""
        rows = [{"id": "live", "status": "running", "created_at": "2026-06-02"},
                {"id": "done", "status": "complete", "created_at": "2026-06-01"}]
        assert self._latest(rows)["id"] == "done"

    def test_a_running_assessment_is_returned_when_nothing_finished(self):
        rows = [{"id": "live", "status": "running", "created_at": "2026-06-02"}]
        assert self._latest(rows)["id"] == "live"

    def test_no_assessments_returns_none_rather_than_raising(self):
        assert self._latest([]) is None

    def test_a_wrapped_payload_shape_is_unwrapped(self):
        rows = {"data": [{"id": "a", "status": "complete", "created_at": "2026-01-01"}]}
        assert self._latest(rows)["id"] == "a"


class TestTheCommandNoLongerSendsNone:
    def test_cmd_ci_resolves_before_fetching(self):
        m = re.search(r"^def cmd_ci\(args\):(.*?)(?=^def )", SRC, re.S | re.M)
        assert m, "cmd_ci not found"
        body = m.group(1)
        assert "latest_assessment" in body, (
            "cmd_ci fetches an assessment without resolving one — with --app alone it will put "
            "the literal string 'None' in the URL and 404 on every app")

    def test_it_no_longer_passes_the_raw_flag_straight_through(self):
        m = re.search(r"^def cmd_ci\(args\):(.*?)(?=^def )", SRC, re.S | re.M)
        assert "c.get_assessment(_resolve_app(c, args.app), args.assessment)" not in m.group(1)

    def test_a_missing_app_is_a_readable_error(self):
        """`ci --baseline x.json` printed a bare 'no application given' with no next step."""
        m = re.search(r"^def cmd_ci\(args\):(.*?)(?=^def )", SRC, re.S | re.M)
        assert "--file" in m.group(1), "the no-app error should name the flag that gates a saved result"

    def test_an_app_with_no_runs_says_so(self):
        m = re.search(r"^def cmd_ci\(args\):(.*?)(?=^def )", SRC, re.S | re.M)
        assert "no assessments yet" in m.group(1)


class TestTheFloorIsWiredIn:
    def test_cmd_ci_derives_the_floor_from_the_app(self):
        m = re.search(r"^def cmd_ci\(args\):(.*?)(?=^def )", SRC, re.S | re.M)
        assert "credible_probe_floor" in m.group(1), (
            "cmd_ci still uses the fixed floor, so a scoped single-control run cannot pass")

    def test_an_explicit_min_probes_still_wins(self):
        m = re.search(r"^def cmd_ci\(args\):(.*?)(?=^def )", SRC, re.S | re.M)
        assert "args.min_probes is None" in m.group(1), (
            "--min-probes 0 is the documented override and must not be overridden by the "
            "derived floor")

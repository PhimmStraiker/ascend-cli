"""
The CI gate must not go green on a run that measured nothing.

A bridge that is not running produces the tool's signature failure: probes go unanswered,
unanswered probes are not findings, and the assessment completes with a perfect score having
tested nothing. `ascend reports` has always flagged this with `!!`. The gate exited 0 on it — so
the report a human reads warned about the run while the pipeline it feeds went green.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from reporting import ci  # noqa: E402


def assessment(*, total, failed=0, status="complete", controls=None, severity="low"):
    return {
        "status": status, "score": 0 if not failed else 40, "severity": severity,
        "total": total, "failed": failed,
        "category_summary": [{"name": "data_leak", "controls": controls if controls is not None
                              else [{"id": "phone_number", "status": "pass", "severity": "low",
                                     "failed": 0, "total": total}]}],
    }


FAILING_CONTROL = [{"id": "phone_number", "status": "fail", "severity": "high",
                    "failed": 1, "total": 2, "keyfindings": ["leaked"]}]


class TestTinyCleanRunIsNotAPass:
    def test_a_two_probe_clean_run_is_refused(self):
        r = ci.gate(assessment(total=2))
        assert r["unreadable"] is True
        assert r["exit_code"] != 0
        assert "only 2 probe" in r["reasons"][0]
        assert "bridge" in r["reasons"][0].lower()

    def test_it_is_reported_as_untrustworthy_not_as_findings(self):
        """The distinction matters: 'the target answered badly' and 'we learned nothing' need
        different responses from whoever reads the pipeline."""
        r = ci.gate(assessment(total=2))
        assert r["unreadable"] is True
        assert r["threshold_breaches"] == []

    @pytest.mark.parametrize("total", [0, 1, 2, 3, 4])
    def test_every_count_below_the_floor_is_refused(self, total):
        assert ci.gate(assessment(total=total))["unreadable"] is True

    def test_a_normal_clean_run_still_passes(self):
        r = ci.gate(assessment(total=100))
        assert r["exit_code"] == 0
        assert not r.get("unreadable")

    def test_the_floor_is_the_documented_default(self):
        assert ci.MIN_CREDIBLE_PROBES == 5
        assert ci.gate(assessment(total=5))["exit_code"] == 0

    def test_min_probes_zero_opts_out(self):
        """Some runs are legitimately tiny; the operator can say so explicitly."""
        r = ci.gate(assessment(total=2), min_probes=0)
        assert r["exit_code"] == 0
        assert not r.get("unreadable")

    def test_a_tiny_run_WITH_a_finding_is_a_findings_failure(self):
        """It measured something. That is a real result, not an untrustworthy one."""
        r = ci.gate(assessment(total=2, failed=1, controls=FAILING_CONTROL),
                    fail_on_severity="high")
        assert not r.get("unreadable"), "it found something — the run clearly worked"
        assert r["exit_code"] != 0
        assert any("phone_number" in x for x in r["reasons"])

    def test_an_unfinished_run_is_refused_for_being_unfinished(self):
        """A run still going has few probes SO FAR; that is not evidence of a dead bridge.

        It is still refused — gating a partial run is its own false clean — but the diagnosis
        must be the honest one, or the operator goes looking for a bridge problem that is not
        there.
        """
        r = ci.gate(assessment(total=2, status="running"))
        assert r["exit_code"] == 1
        assert "not finished" in r["reasons"][0]
        assert "probe" not in r["reasons"][0], "wrong diagnosis sends the operator hunting"

    def test_a_missing_probe_count_does_not_crash_or_falsely_accuse(self):
        a = assessment(total=100)
        del a["total"]
        r = ci.gate(a)
        assert r["exit_code"] == 0


class TestGateAndReportAgree:
    def test_both_flag_the_same_run(self):
        """The regression: reports warned, ci passed."""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "ascend_cli", REPO / "shells" / "cli" / "ascend.py")
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)

        a = assessment(total=2)
        assert cli._false_pass_warning(a), "reports should flag it"
        assert ci.gate(a)["unreadable"] is True, "and so should the gate"


class TestUnreadableStillHandled:
    def test_completed_with_no_categories_is_still_unreadable(self):
        r = ci.gate({"status": "complete", "total": 100, "category_summary": []})
        assert r["unreadable"] is True

    def test_that_check_runs_before_the_probe_count_check(self):
        """No category data is the more specific diagnosis; it should win."""
        r = ci.gate({"status": "complete", "total": 2, "category_summary": []})
        assert "category_summary" in r["reasons"][0]

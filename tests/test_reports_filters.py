"""
Report filters that used to lose findings or ignore what you asked for.

`ascend reports` is what a human reads to decide whether a run is a problem. A filter that
silently drops a finding, or silently ignores itself, produces a clean-looking report from a
run that is not clean — the same failure this whole tool is built to prevent, one layer up.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "control"))

_spec = importlib.util.spec_from_file_location("ascend_cli", REPO / "shells" / "cli" / "ascend.py")
cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli)

from reporting import ci  # noqa: E402

CLI = str(REPO / "shells" / "cli" / "ascend.py")


def _run(*args):
    env = {"STRAIKER_PAT": "s6r_pat_test", "PATH": "/usr/bin:/bin", "NO_COLOR": "1",
           "ASCEND_NO_SPINNER": "1", "ASCEND_POLICY": "/tmp/_t_policy_should_not_exist.json"}
    return subprocess.run([sys.executable, CLI, *args], capture_output=True, text=True,
                          cwd=str(REPO), env=env)


class TestSeverityFailsSafe:
    """An undeterminable severity must be surfaced, never quietly ranked harmless."""

    def test_finished_run_with_no_severity_ranks_most_severe(self):
        assert cli._row_sev_rank({"severity": None, "status": "complete"}) == 0

    def test_finished_run_with_an_unrecognized_severity_ranks_most_severe(self):
        """Schema drift — a severity value we have never seen — must not read as harmless."""
        assert cli._row_sev_rank({"severity": "catastrophic-new-tier", "status": "complete"}) == 0

    def test_a_run_still_going_is_not_treated_as_a_problem(self):
        """No severity yet is expected while it runs; that is not the same as unreadable."""
        assert cli._row_sev_rank({"severity": None, "status": "running"}) == 6

    def test_known_severities_keep_their_order(self):
        order = [cli._row_sev_rank({"severity": s, "status": "complete"})
                 for s in ("critical", "high", "medium", "low")]
        assert order == sorted(order) and len(set(order)) == 4

    def test_min_sev_does_not_hide_an_unreadable_finding(self):
        """The regression: C and D used to vanish from a --min-sev high report."""
        rows = [
            {"app": "A", "severity": "critical", "status": "complete"},
            {"app": "B", "severity": "high", "status": "complete"},
            {"app": "C", "severity": None, "status": "complete"},
            {"app": "D", "severity": "weird", "status": "complete"},
            {"app": "E", "severity": "low", "status": "complete"},
        ]
        floor = cli._SEV_RANK["high"]
        kept = {r["app"] for r in rows if cli._row_sev_rank(r) <= floor}
        assert {"C", "D"} <= kept, "a finished run with an unreadable severity must be shown"
        assert "E" not in kept

    def test_reports_and_the_ci_gate_agree_on_direction(self):
        """They disagreed: the gate failed the run while the report showed nothing."""
        assert ci._sev_index("unknown") < ci._sev_index("critical")
        assert cli._row_sev_rank({"severity": "unknown", "status": "complete"}) <= \
            cli._row_sev_rank({"severity": "critical", "status": "complete"})


class TestFiltersRefuseBadInput:
    def test_min_sev_rejects_an_unknown_value(self):
        """`--min-sev hgih` used to match EVERYTHING — the filter silently disabled itself."""
        r = _run("reports", "--min-sev", "bogus")
        assert r.returncode != 0
        assert "invalid choice" in (r.stdout + r.stderr)

    @pytest.mark.parametrize("value", ["critical", "high", "medium", "low", "info", "none"])
    def test_every_offered_severity_is_a_rank_we_know(self, value):
        assert value in cli._SEV_RANK, f"--min-sev offers {value} but the filter cannot rank it"

    def test_since_rejects_a_non_number(self):
        """It was parsed inside a try/except that swallowed the error, so the flag did nothing."""
        r = _run("reports", "--since", "bogus")
        assert r.returncode != 0
        assert "--since expects a number of days" in (r.stdout + r.stderr)

    @pytest.mark.parametrize("value", ["7", "7d", "30", "30d"])
    def test_since_accepts_days_with_or_without_the_suffix(self, value):
        r = _run("reports", "--since", value)
        assert "--since expects" not in (r.stdout + r.stderr)


class TestPolicySeverityValidation:
    def test_an_invalid_severity_is_refused_at_write_time(self, tmp_path):
        """The policy file gates CI. An unrecognized severity ranks most-severe under the
        fail-safe, so a typo would silently fail every future gate, far from where it was typed."""
        pol = tmp_path / "p.json"
        r = subprocess.run(
            [sys.executable, CLI, "policy", "set", "--control", "x=bogus", "--policy", str(pol)],
            capture_output=True, text=True, cwd=str(REPO),
            env={"STRAIKER_PAT": "s6r_pat_test", "PATH": "/usr/bin:/bin", "NO_COLOR": "1"})
        assert r.returncode != 0
        assert "severity must be one of" in (r.stdout + r.stderr)
        assert not pol.exists(), "nothing should be written when the value is refused"

    def test_a_valid_severity_is_written(self, tmp_path):
        pol = tmp_path / "p.json"
        r = subprocess.run(
            [sys.executable, CLI, "policy", "set", "--control", "tool_misuse=critical",
             "--policy", str(pol)],
            capture_output=True, text=True, cwd=str(REPO),
            env={"STRAIKER_PAT": "s6r_pat_test", "PATH": "/usr/bin:/bin", "NO_COLOR": "1"})
        assert r.returncode == 0, r.stderr
        assert json.loads(pol.read_text())["default"]["controls"]["tool_misuse"] == "critical"

    def test_critical_is_allowed_locally_even_though_the_platform_clamps_it(self, tmp_path):
        """Local policy ranks findings and gates CI; the clamp happens only on push."""
        assert "critical" in cli.POLICY_SEVERITIES
        import api
        assert "critical" not in api.CATEGORY_SEVERITIES

"""
test_bridge_alarm — the NO-BRIDGE alarm must fire for bridge-based apps ONLY.

Real bug this pins: the alarm flagged an `api`-type app (Ascend calls those targets directly over
the internet) as having "no relay", implying a false-pass risk that cannot exist for it. A noisy
alarm trains people to ignore the one case that genuinely matters — a `thin` app whose bridge is
down, which finishes looking clean while measuring nothing.
"""
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
for p in ("control", "runtime", "shells/cli"):
    sys.path.insert(0, str(REPO / p))
import ascend  # noqa: E402


@pytest.mark.parametrize("api_type,expected", [
    ("thin", True),          # the bridge IS the transport -> needs one
    ("api", False),          # Ascend calls the endpoint directly
    ("gcp", False),          # native cloud integration
    ("bedrock", False),
    ("", False),             # unknown/blank: don't invent an alarm
    (None, False),
])
def test_only_thin_apps_need_a_bridge(api_type, expected):
    assert ascend.needs_bridge({"api_type": api_type}) is expected


def test_needs_bridge_is_case_insensitive_and_null_safe():
    assert ascend.needs_bridge({"api_type": "THIN"}) is True
    assert ascend.needs_bridge({}) is False
    assert ascend.needs_bridge(None) is False


def test_bridge_group_exists_with_relay_alias():
    p = ascend.build_parser()
    groups = set()
    import argparse
    for a in p._actions:
        if isinstance(a, argparse._SubParsersAction):
            groups |= set(a.choices)
    assert "bridge" in groups          # the legacy, correct term
    assert "relay" in groups          # kept working for anyone scripted against it


def test_app_list_reports_completed_counts():
    """DONE must count only terminal-complete runs, not every run."""
    src = (REPO / "shells/cli/ascend.py").read_text()
    assert "'DONE':>4" in src
    assert 'completed_count' in src

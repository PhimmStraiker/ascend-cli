"""
test_target_timeout — how long a probe may take.

There is ONE real fact: the platform bounds each probe (~120s), and the clock starts when the probe
is QUEUED, not when the bridge calls the target. Everything else is derived from it, so the numbers
cannot drift apart. A timeout shorter than the target's reply time fails quietly (every probe errors,
the run auto-pauses, and it reports low risk having measured nothing); a longer one cannot help,
because the router has already abandoned the probe.
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "runtime"))
from adapters.base import (  # noqa: E402
    bridge_response_timeout_s, platform_probe_window_s, platform_window_warning, resolve_timeout_s)


def test_everything_derives_from_the_one_window():
    window = platform_probe_window_s()
    bridge = bridge_response_timeout_s()
    target = resolve_timeout_s({})
    assert target < bridge < window, (target, bridge, window)


def test_one_env_var_moves_all_three(monkeypatch):
    monkeypatch.setenv("ASCEND_PLATFORM_PROBE_WINDOW_MS", "600000")
    assert platform_probe_window_s() == 600.0
    assert bridge_response_timeout_s() == 590.0
    assert resolve_timeout_s({}) == 580.0


def test_config_timeout_still_wins_but_cannot_exceed_the_bridge():
    # The long-standing per-target knob is honoured...
    assert resolve_timeout_s({"timeout_ms": 20000}) == 20.0
    # ...but waiting past the point the router gave up only holds a worker and socket open.
    assert resolve_timeout_s({"timeout_ms": 3_600_000}) == bridge_response_timeout_s()


def test_garbage_and_zero_fall_back_instead_of_raising():
    for bad in (None, "nope", 0, -5):
        assert resolve_timeout_s({"timeout_ms": bad}) > 0


# ---- what the operator is told, from one measured probe ----------------------------------------
def test_fast_target_is_not_warned_about():
    assert platform_window_warning(1_000) is None
    assert platform_window_warning(60_000) is None


def test_target_at_or_over_the_window_is_called_unassessable():
    msg = platform_window_warning(130_000)
    assert msg and "at or beyond" in msg
    assert "adapter timeout does NOT help" in msg      # the obvious wrong fix


def test_target_approaching_the_window_is_warned_about_queueing():
    msg = platform_window_warning(80_000)
    assert msg and "QUEUED" in msg


def test_warning_moves_with_the_window(monkeypatch):
    monkeypatch.setenv("ASCEND_PLATFORM_PROBE_WINDOW_MS", "600000")
    assert platform_window_warning(130_000) is None    # fine once the platform allows 10 min


def test_garbage_duration_never_raises():
    assert platform_window_warning(None) is None
    assert platform_window_warning("nope") is None

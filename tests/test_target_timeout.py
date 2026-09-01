"""
test_target_timeout — how long the bridge waits for ONE reply from the target.

This exists because a short fixed timeout is indistinguishable, in the results, from a target that
refused: measured live, a 110s agent under a 20s config timeout failed EVERY probe, the platform
then auto-paused the assessment, and the run presented as "the bridge broke". Agentic targets
routinely take 2-3 minutes and some take much longer, so the default must be generous, the operator
must be able to tune it without editing code, and there must still be a ceiling — a hung target
must not pin a worker open forever.
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "runtime"))
from adapters.base import (  # noqa: E402
    DEFAULT_TARGET_TIMEOUT_MS, MAX_TARGET_TIMEOUT_MS, resolve_timeout_s)


def test_default_is_sized_for_an_agentic_target():
    # No config value: must comfortably clear the common 2-3 minute agentic reply.
    assert resolve_timeout_s({}) >= 180.0
    assert resolve_timeout_s(None) == DEFAULT_TARGET_TIMEOUT_MS / 1000.0


def test_config_value_wins():
    assert resolve_timeout_s({"timeout_ms": 20000}) == 20.0


def test_excessive_values_are_clamped():
    # Slow is fine; hung is not. An hour-long timeout would hold a worker open all run.
    assert resolve_timeout_s({"timeout_ms": 3_600_000}) == MAX_TARGET_TIMEOUT_MS / 1000.0


def test_env_tunes_both_ends_without_a_code_change(monkeypatch):
    monkeypatch.setenv("ASCEND_TARGET_TIMEOUT_MS", "600000")
    assert resolve_timeout_s({}) == 600.0
    monkeypatch.setenv("ASCEND_TARGET_MAX_TIMEOUT_MS", "1200000")
    assert resolve_timeout_s({"timeout_ms": 3_600_000}) == 1200.0


def test_garbage_falls_back_instead_of_raising():
    # A malformed config must not take the bridge down mid-run.
    assert resolve_timeout_s({"timeout_ms": "not-a-number"}) == DEFAULT_TARGET_TIMEOUT_MS / 1000.0
    assert resolve_timeout_s({"timeout_ms": None}) == DEFAULT_TARGET_TIMEOUT_MS / 1000.0


def test_never_returns_zero_or_negative():
    assert resolve_timeout_s({"timeout_ms": 0}) > 0
    assert resolve_timeout_s({"timeout_ms": -5}) > 0

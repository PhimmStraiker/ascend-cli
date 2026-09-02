"""
test_target_timeout — how long the bridge waits for ONE reply from the target.

Two failures shaped these rules. A timeout set SHORTER than the target's reply time fails quietly:
every probe errors, the platform auto-pauses the run, and the assessment reports low risk while
measuring nothing. But a timeout set far LONGER is not the fix either — the bridge abandons a probe
at the platform's response window, so the extra time is never waited through the bridge, and the
HTTP call keeps running after the router gave up, holding a worker and a socket.

So the target timeout is bounded by the bridge window rather than chosen freely, and the two
numbers must stay consistent — that consistency is asserted here, because they drifted apart once.
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "runtime"))
from adapters.base import (  # noqa: E402
    DEFAULT_TARGET_TIMEOUT_MS, MAX_TARGET_TIMEOUT_MS, resolve_ms, resolve_timeout_s)


def test_default_does_not_exceed_the_bridge_window():
    # The bridge gives up at the platform's response window. A default above it would never be
    # waited through the bridge and would leave the HTTP call running past the router's deadline.
    from call_target import _DEFAULT_BRIDGE_RESPONSE_TIMEOUT_S
    assert DEFAULT_TARGET_TIMEOUT_MS / 1000.0 <= _DEFAULT_BRIDGE_RESPONSE_TIMEOUT_S


def test_default_applies_when_nothing_is_configured():
    assert resolve_timeout_s({}) == DEFAULT_TARGET_TIMEOUT_MS / 1000.0
    assert resolve_timeout_s(None) == DEFAULT_TARGET_TIMEOUT_MS / 1000.0


def test_config_value_wins():
    assert resolve_timeout_s({"timeout_ms": 20000}) == 20.0


def test_excessive_values_are_clamped():
    # Slow is fine; hung is not. An hour-long timeout would hold a worker for the whole run.
    assert resolve_timeout_s({"timeout_ms": 3_600_000}) == MAX_TARGET_TIMEOUT_MS / 1000.0


def test_env_tunes_both_ends_without_a_code_change(monkeypatch):
    monkeypatch.setenv("ASCEND_TARGET_TIMEOUT_MS", "300000")
    assert resolve_timeout_s({}) == 300.0
    monkeypatch.setenv("ASCEND_TARGET_MAX_TIMEOUT_MS", "1200000")
    assert resolve_timeout_s({"timeout_ms": 3_600_000}) == 1200.0


def test_garbage_falls_back_instead_of_raising():
    # A malformed config must not take the bridge down mid-run.
    assert resolve_timeout_s({"timeout_ms": "not-a-number"}) == DEFAULT_TARGET_TIMEOUT_MS / 1000.0
    assert resolve_timeout_s({"timeout_ms": None}) == DEFAULT_TARGET_TIMEOUT_MS / 1000.0


def test_never_returns_zero_or_negative():
    assert resolve_timeout_s({"timeout_ms": 0}) > 0
    assert resolve_timeout_s({"timeout_ms": -5}) > 0


# ---- the shared precedence rule (one implementation, used by both resolvers) -------------------
def test_resolve_ms_precedence(monkeypatch):
    assert resolve_ms({"k": 5000}, "k", "SOME_ENV", 9000) == 5000        # config wins
    monkeypatch.setenv("SOME_ENV", "7000")
    assert resolve_ms({}, "k", "SOME_ENV", 9000) == 7000                 # then env
    assert resolve_ms({}, "k", "UNSET_ENV_NAME", 9000) == 9000           # then default
    assert resolve_ms(None, None, "UNSET_ENV_NAME", 9000) == 9000        # ceiling-style lookup


def test_bridge_window_uses_the_same_rule(monkeypatch):
    from call_target import _bridge_response_timeout_s, _DEFAULT_BRIDGE_RESPONSE_TIMEOUT_S
    assert _bridge_response_timeout_s({}) == _DEFAULT_BRIDGE_RESPONSE_TIMEOUT_S
    assert _bridge_response_timeout_s({"bridge_response_timeout_ms": 300000}) == 300.0
    monkeypatch.setenv("ASCEND_BRIDGE_RESPONSE_TIMEOUT_MS", "240000")
    assert _bridge_response_timeout_s({}) == 240.0

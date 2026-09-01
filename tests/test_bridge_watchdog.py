"""
test_bridge_watchdog — `_supervise_bridge`, the per-poll watchdog that keeps a bridge alive for the
lifetime of a followed `assess run`. If the relay dies mid-run for ANY reason, the next poll tick
restarts it, so a customer never has to write their own watchdog. Properties: restart a dead bridge,
never touch a healthy one, skip native apps, report a failed restart, and never raise (supervision
must not break the run's poll loop).
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
for p in ("control", "runtime", "shells/cli"):
    sys.path.insert(0, str(REPO / p))
import ascend  # noqa: E402

BRIDGE_APP = {"id": "aapp_x", "api_type": "thin"}


def _setup(monkeypatch, *, serving, ensure_result=None, needs=True):
    import supervisor
    monkeypatch.setattr(supervisor, "is_serving", lambda app_id: serving)
    monkeypatch.setattr(ascend, "needs_bridge", lambda app: needs)
    calls = {"ensure": 0}

    def fake_ensure(c, app, *, assessment_id=None, args=None):
        calls["ensure"] += 1
        return ensure_result or {}
    monkeypatch.setattr(ascend, "_ensure_bridge", fake_ensure)
    return calls


def test_watchdog_restarts_a_dead_bridge(monkeypatch):
    calls = _setup(monkeypatch, serving=False, ensure_result={"started": True, "pid": 4242})
    note = ascend._supervise_bridge(None, BRIDGE_APP, args=None)
    assert calls["ensure"] == 1                       # it re-ensured the down bridge
    assert note and "restarted" in note and "4242" in note


def test_watchdog_noop_when_bridge_is_serving(monkeypatch):
    calls = _setup(monkeypatch, serving=True)
    note = ascend._supervise_bridge(None, BRIDGE_APP, args=None)
    assert note is None
    assert calls["ensure"] == 0                       # never touches a healthy bridge


def test_watchdog_skips_native_app(monkeypatch):
    calls = _setup(monkeypatch, serving=False, needs=False)
    note = ascend._supervise_bridge(None, {"id": "aapp_n", "api_type": "api"}, args=None)
    assert note is None
    assert calls["ensure"] == 0


def test_watchdog_reports_restart_failure(monkeypatch):
    _setup(monkeypatch, serving=False, ensure_result={"error": "no stored bridge key"})
    note = ascend._supervise_bridge(None, BRIDGE_APP, args=None)
    assert note and note.startswith("!") and "no stored bridge key" in note


def test_watchdog_never_raises(monkeypatch):
    import supervisor
    monkeypatch.setattr(supervisor, "is_serving", lambda app_id: False)
    monkeypatch.setattr(ascend, "needs_bridge", lambda app: True)

    def boom(*a, **k):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(ascend, "_ensure_bridge", boom)
    note = ascend._supervise_bridge(None, BRIDGE_APP, args=None)
    assert note and "watchdog error" in note          # swallowed, run's poll loop survives

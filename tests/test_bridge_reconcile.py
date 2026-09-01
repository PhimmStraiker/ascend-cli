"""
test_bridge_reconcile — the self-reconciling bridge's stop/serve decision, and the CLI type label.

The overriding property is FALSE-PASS SAFETY: the bridge must NEVER self-stop while it cannot verify
its work is done. An unanswered probe scores a clean pass, so the safe direction is always "keep
serving." A run is over ONLY when it is EXPLICITLY terminal (a status in api.TERMINAL_STATUSES); any
status the CLI does not enumerate — e.g. an intermediate recon-phase state — is NOT terminal and must
keep the bridge alive. Even a genuinely-terminal run rides out a termination grace, so a transient gap
between recon rounds does not reap the bridge.
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
for p in ("control", "runtime", "shells/cli"):
    sys.path.insert(0, str(REPO / p))
import ascend  # noqa: E402
import api      # noqa: E402

D = ascend._reconcile_decision
IDLE = 1800
NOW = 100_000.0          # well past the startup grace unless a test says otherwise
GRACE = ascend._TERMINATION_GRACE_S


def _dec(assessments, *, now=NOW, started_at=0.0, last_probe_ts=0.0, control_ok=True,
         idle_timeout_s=IDLE, terminal_since=None):
    return D(assessments, now=now, started_at=started_at, last_probe_ts=last_probe_ts,
             idle_timeout_s=idle_timeout_s, control_ok=control_ok, terminal_since=terminal_since)


# ---- false-pass safety: never self-kill when state is unknown ---------------------------------
def test_cannot_read_platform_always_serves():
    assert _dec([{"status": "completed"}], control_ok=False) == "serve"


def test_startup_grace_serves_even_with_no_assessments():
    assert _dec([], now=50.0, started_at=0.0) == "serve"


# ---- the bug that killed the bridge mid-recon (regression) -------------------------------------
def test_unknown_recon_status_never_reaps():
    # The reconcile used to reap ANY status not in a hand-written running allowlist. A recon-phase
    # status the platform emits between rounds is neither running nor terminal, and MUST keep the
    # bridge serving. This is the exact failure a design partner hit: bridge dies after each round.
    for status in ["reconnaissance", "recon", "analyzing", "scanning", "scoring",
                   "generating", "pending", "initializing", "processing", "queued_recon"]:
        assert status.lower() not in api.TERMINAL_STATUSES          # genuinely non-terminal
        # serves regardless of terminal_since, because a non-terminal assessment is present
        assert _dec([{"status": status}], terminal_since=NOW - 9999) == "serve", status


# ---- ordinary lifecycle: stop only on a DURABLY terminal run -----------------------------------
def test_all_terminal_past_grace_stops():
    assert _dec([{"status": "completed"}, {"status": "failed"}],
                terminal_since=NOW - (GRACE + 10)) == "stop-terminal"


def test_all_terminal_within_grace_serves():
    # all terminal, but only just now -> ride out the grace (could be a between-round gap)
    assert _dec([{"status": "completed"}], terminal_since=NOW - 1) == "serve"


def test_terminal_since_none_serves():
    # the caller has not yet observed the all-terminal condition -> never stop this tick
    assert _dec([{"status": "completed"}], terminal_since=None) == "serve"


def test_no_assessments_past_grace_stops():
    assert _dec([], terminal_since=NOW - (GRACE + 10)) == "stop-terminal"


def test_no_assessments_within_grace_serves():
    assert _dec([], terminal_since=NOW - 1) == "serve"


def test_running_serves():
    assert _dec([{"status": "running"}]) == "serve"


def test_queued_and_in_progress_serve():
    assert _dec([{"status": "queued"}]) == "serve"
    assert _dec([{"status": "in_progress"}]) == "serve"


# ---- default: stop only on terminal, never idle-kill ------------------------------------------
def test_idle_kill_disabled_by_default_serves():
    assert _dec([{"status": "paused"}], last_probe_ts=NOW - 999999, idle_timeout_s=0) == "serve"


def test_created_stalled_never_reaped():
    assert _dec([{"status": "created"}], last_probe_ts=0.0, idle_timeout_s=IDLE) == "serve"
    assert _dec([{"status": "created"}], last_probe_ts=NOW - 999999, idle_timeout_s=IDLE) == "serve"


def test_paused_never_probed_serves():
    assert _dec([{"status": "paused"}], last_probe_ts=0.0, idle_timeout_s=IDLE) == "serve"


# ---- opt-in idle cleanup: only a genuinely paused, already-probed, then-quiet run --------------
def test_paused_probed_and_idle_stops_when_opted_in():
    assert _dec([{"status": "paused"}], last_probe_ts=NOW - 2000, idle_timeout_s=IDLE) == "stop-idle"


def test_paused_but_recent_probe_serves():
    assert _dec([{"status": "paused"}], last_probe_ts=NOW - 1000, idle_timeout_s=IDLE) == "serve"


# ---- per-app: a shared bridge stays up while ANY assessment is non-terminal --------------------
def test_mixed_terminal_and_running_serves():
    assert _dec([{"status": "completed"}, {"status": "running"}]) == "serve"


def test_mixed_paused_and_running_serves():
    assert _dec([{"status": "paused"}, {"status": "running"}], last_probe_ts=0.0) == "serve"


# ---- lifecycle: drive the REAL reconcile step through a recon run, carrying terminal_since ------
def test_reconcile_step_rides_through_recon_lifecycle():
    step = ascend._reconcile_step
    t, ts = 1000.0, None
    # multiple recon rounds: running probes, an intermediate status between rounds, then a brief
    # gap with no assessment for one tick — every one of these used to kill the bridge.
    for snap in ([{"status": "running"}], [{"status": "reconnaissance"}], [{"status": "running"}],
                 [{"status": "analyzing"}], [{"status": "running"}], [], [{"status": "running"}]):
        t += 30.0
        decision, ts = step(snap, now=t, started_at=0.0, last_probe_ts=t - 5,
                            idle_timeout_s=0, control_ok=True, terminal_since=ts)
        assert decision == "serve", (snap, decision)
    # the run genuinely completes and STAYS completed past the grace -> stop exactly once
    stopped_at = None
    for _ in range(int(GRACE / 30) + 3):
        t += 30.0
        decision, ts = step([{"status": "completed"}], now=t, started_at=0.0, last_probe_ts=t - 5,
                            idle_timeout_s=0, control_ok=True, terminal_since=ts)
        if decision == "stop-terminal":
            stopped_at = t
            break
    assert stopped_at is not None, "bridge never stopped on a durably completed run"


def test_reconcile_step_resets_grace_when_a_new_round_starts():
    step = ascend._reconcile_step
    # a completed run starts the grace clock...
    _, ts = step([{"status": "completed"}], now=1000.0, started_at=0.0, last_probe_ts=990.0,
                 idle_timeout_s=0, control_ok=True, terminal_since=None)
    assert ts == 1000.0
    # ...but well past the grace a NEW round appears -> grace resets, bridge keeps serving
    d, ts = step([{"status": "running"}], now=1000.0 + GRACE + 100, started_at=0.0,
                 last_probe_ts=1000.0 + GRACE + 90, idle_timeout_s=0, control_ok=True,
                 terminal_since=ts)
    assert d == "serve" and ts is None


# ---- the CLI-facing type label (wire 'thin' -> shown 'bridge') ---------------------------------
def test_type_label_maps_thin_to_bridge():
    assert ascend._type_label("thin") == "bridge"
    assert ascend._type_label("THIN") == "bridge"


def test_type_label_passes_through_native_types():
    assert ascend._type_label("api") == "api"
    assert ascend._type_label("gcp") == "gcp"
    assert ascend._type_label("bedrock") == "bedrock"


def test_type_label_is_null_safe():
    assert ascend._type_label(None) == "?"
    assert ascend._type_label("") == "?"

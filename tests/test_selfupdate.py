"""
test_selfupdate — the pure version-comparison used by `ascend doctor`.

No network: `check()` takes an injected `fetch_latest` callable, so every case is a fixture. The
load-bearing properties: a local build AHEAD of the latest release never nudges; a min-supported
breach escalates to a recommended/security update; opt-out beats everything (including a pending
security update) and never even fetches; and any fetch failure is swallowed to `unknown` without
raising or changing behaviour.
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "runtime"))
import selfupdate as su  # noqa: E402


def rel(tag, *, body=None, name=None, url="https://example/rel"):
    return {"tag": tag, "body": body, "name": name, "url": url}


# ---- semver parse/compare -----------------------------------------------------------------------
def test_parse_version_forms():
    assert su.parse_version("1.2.3") == (1, 2, 3)
    assert su.parse_version("v1.2.3") == (1, 2, 3)
    assert su.parse_version("v1.2.3-rc1") == (1, 2, 3)   # pre-release suffix ignored
    assert su.parse_version("garbage") is None
    assert su.parse_version(None) is None


def test_cmp_versions():
    assert su.cmp_versions("1.0.0", "1.0.1") == -1
    assert su.cmp_versions("1.0.1", "1.0.0") == 1
    assert su.cmp_versions("1.0.0", "v1.0.0") == 0
    assert su.cmp_versions("nope", "1.0.0") is None


def test_min_supported_parse():
    assert su.min_supported_from_body("blah\nmin-supported: 1.2.0\nmore") == "1.2.0"
    assert su.min_supported_from_body("Min_Supported = v2.0.0") == "2.0.0"
    assert su.min_supported_from_body("X-Ascend-Min-Supported: 1.5.0") == "1.5.0"
    assert su.min_supported_from_body("nothing here") is None
    assert su.min_supported_from_body(None) is None


# ---- check(): the state machine -----------------------------------------------------------------
def test_equal_is_up_to_date():
    assert su.check("1.0.0", lambda: rel("v1.0.0"), env={})["state"] == "up_to_date"


def test_local_build_ahead_is_not_nudged():
    # a dev/local build past the latest release must never be told to "update" backwards
    assert su.check("1.1.0", lambda: rel("v1.0.0"), env={})["state"] == "up_to_date"


def test_behind_no_min_is_soft_update_available():
    v = su.check("1.0.0", lambda: rel("v1.1.0", body="routine notes"), env={})
    assert v["state"] == "update_available"
    assert v["severity"] == "none"
    assert v["latest"] == "v1.1.0"


def test_behind_but_at_or_above_min_stays_soft():
    # min-supported present but not breached -> still just informational
    v = su.check("1.1.0", lambda: rel("v1.2.0", body="min-supported: 1.0.0"), env={})
    assert v["state"] == "update_available"
    assert v["severity"] == "none"


def test_below_min_supported_is_recommended_security():
    v = su.check("1.0.0", lambda: rel("v1.2.0", body="min-supported: 1.1.0"), env={})
    assert v["state"] == "update_recommended"
    assert v["severity"] == "security"
    assert v["min_supported"] == "1.1.0"


def test_security_token_escalates():
    v = su.check("1.0.0", lambda: rel("v1.1.0", name="[security] bridge fix"), env={})
    assert v["state"] == "update_recommended"
    assert v["severity"] == "security"


def test_no_release_state():
    assert su.check("1.0.0", lambda: {"no_release": True}, env={})["state"] == "no_release"


def test_unreachable_is_unknown():
    v = su.check("1.0.0", lambda: None, env={})
    assert v["state"] == "unknown"
    assert v["reason"] == "could not reach GitHub"


def test_fetch_raising_is_swallowed():
    def boom():
        raise RuntimeError("dns down")
    v = su.check("1.0.0", boom, env={})
    assert v["state"] == "unknown"
    assert "RuntimeError" in (v["reason"] or "")


def test_unparseable_latest_is_unknown_not_crash():
    v = su.check("1.0.0", lambda: rel("weird-tag"), env={})
    assert v["state"] == "unknown"


# ---- opt-out: the config-matrix pair that must hold (opt-out beats severity) ---------------------
def test_opt_out_skips_and_never_fetches():
    called = []

    def fetch():
        called.append(1)
        return rel("v9.9.9")
    v = su.check("1.0.0", fetch, env={su.OPT_OUT_ENV: "1"})
    assert v["state"] == "skipped"
    assert called == []            # opt-out short-circuits before any network attempt


def test_opt_out_beats_a_pending_security_update():
    # even with a min-supported breach queued, opt-out wins and nothing is fetched
    v = su.check("1.0.0", lambda: rel("v2.0.0", body="min-supported: 2.0.0"),
                 env={su.OPT_OUT_ENV: "yes"})
    assert v["state"] == "skipped"
    assert v["severity"] == "none"


# ---- install detection + upgrade command --------------------------------------------------------
def test_install_kind():
    assert su.install_kind(frozen=True, repo_has_git=False, module_path="/x") == "binary"
    assert su.install_kind(frozen=False, repo_has_git=True, module_path="/h/ascend-cli") == "clone"
    assert su.install_kind(frozen=False, repo_has_git=False,
                           module_path="/u/.local/pipx/venvs/ascend-cli/lib") == "pipx"
    assert su.install_kind(frozen=False, repo_has_git=False,
                           module_path="/opt/py/site-packages/x") == "pipx"
    assert su.install_kind(frozen=False, repo_has_git=False, module_path="/some/src") == "source"


def test_update_command_per_kind():
    assert su.update_command("clone", "/r") == "git -C /r pull --ff-only"
    assert "pipx upgrade" in su.update_command("pipx")
    assert "releases" in su.update_command("binary")
    assert "git pull" in su.update_command("source")

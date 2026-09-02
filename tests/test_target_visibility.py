"""
test_target_visibility — `target` must SHOW the config binding, and must never hide a target
because of it.

Two live defects, both of which made the config model feel unknowable:

  * `target list` printed ADAPTER as "-" whenever the stored record happened to carry no adapter,
    even though the bound config names it and `target show` on the SAME target resolved it fine.
    A list whose columns disagree with the detail view teaches a user that the tool does not know
    what it is bound to.
  * `_load_named_config` ends in `_die`, which raises SystemExit. SystemExit is a BaseException,
    so the `except Exception` guard around the display path never caught it: `target show` on a
    target whose config no longer resolved EXITED with "config not found" instead of showing the
    app id, key and registration. That is exactly when a user most needs to see what is still
    bound — a missing config is the reason no bridge can start.
"""
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
for p in ("control", "runtime", "shells/cli"):
    sys.path.insert(0, str(REPO / p))
import ascend      # noqa: E402


# ---- reading a config for display must never be fatal -----------------------------------------
def test_peek_config_returns_empty_instead_of_exiting(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ASCEND_CONFIG_DIR", str(tmp_path))
    assert ascend._peek_config("no-such-config") == {}          # must not raise SystemExit


def test_peek_config_does_not_raise_systemexit_specifically(tmp_path, monkeypatch):
    """The bug was SystemExit slipping past `except Exception`; pin that exact behaviour."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ASCEND_CONFIG_DIR", str(tmp_path))
    try:
        ascend._peek_config("still-not-there")
    except BaseException as e:                                   # noqa: BLE001 - that is the point
        pytest.fail(f"_peek_config raised {type(e).__name__}; display paths must not exit")


def test_peek_config_reads_a_real_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ASCEND_CONFIG_DIR", str(tmp_path))
    (tmp_path / "mybot.json").write_text(json.dumps({"adapter": "direct_api",
                                                     "endpoint": "https://h/chat"}))
    cfg = ascend._peek_config("mybot")
    assert cfg["adapter"] == "direct_api"


def test_peek_config_survives_malformed_json(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ASCEND_CONFIG_DIR", str(tmp_path))
    (tmp_path / "broken.json").write_text("{not json")
    assert ascend._peek_config("broken") == {}


def test_load_named_config_still_exits_on_the_command_path(tmp_path, monkeypatch):
    """The fatal version must stay fatal — it guards commands that cannot proceed without one."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ASCEND_CONFIG_DIR", str(tmp_path))
    with pytest.raises(SystemExit):
        ascend._load_named_config("definitely-absent")

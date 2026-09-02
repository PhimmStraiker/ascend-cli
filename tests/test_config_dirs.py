"""
test_config_dirs — a config must resolve the same way from every WORKING DIRECTORY.

(test_config_resolution.py covers reference SHAPES — bare name, filename, path.
This file covers WHERE those references are searched.)

The live defect this pins: resolution picked the FIRST config directory that existed and then
searched only inside it. Every checkout of this repo ships `configs/` (the examples), so as soon
as the CLI ran from a checkout, `~/.ascend/configs` became invisible. A target created from one
directory was "config not found" from another, `runtime start` exited before it ever leased, and
the operator saw a bridge that would not stay up — while `ascend keys` reported the app's key just
fine, because keys live in ~/.ascend and are cwd-independent. That asymmetry is what made it read
as a flaky bridge instead of a lookup bug.
"""
import json
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "runtime"))
import configs as C      # noqa: E402


@pytest.fixture()
def two_dirs(tmp_path, monkeypatch):
    """A cwd that HAS a configs/ dir (like any checkout) plus a separate home config dir."""
    work = tmp_path / "work"
    (work / "configs").mkdir(parents=True)
    (work / "configs" / "example-openai.json").write_text('{"adapter": "direct_api"}')

    home = tmp_path / "home"
    (home / ".ascend" / "configs").mkdir(parents=True)
    (home / ".ascend" / "configs" / "mybot.json").write_text('{"adapter": "direct_api", "x": 1}')

    monkeypatch.chdir(work)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))            # Windows
    monkeypatch.delenv("ASCEND_CONFIG_DIR", raising=False)
    monkeypatch.delenv("ASCENDBRIDGE_CONFIG_DIR", raising=False)
    return work, home


def test_home_config_resolves_even_when_cwd_has_a_configs_dir(two_dirs):
    """The regression itself: cwd/configs exists, so the home config used to be unreachable."""
    assert C.resolve_config_path("mybot") is not None
    assert C.load_config("mybot")["x"] == 1


def test_cwd_still_wins_for_the_same_name(two_dirs):
    """Widening the search must not change which file an existing setup already resolved."""
    work, home = two_dirs
    (work / "configs" / "mybot.json").write_text('{"adapter": "direct_api", "x": 99}')
    assert C.load_config("mybot")["x"] == 99                # cwd shadows home, as before


def test_listing_shows_configs_from_every_directory(two_dirs):
    """`adapter configs` and the did-you-mean hint read this; a config you can LOAD must be listed."""
    names = {p.stem for p in C.list_configs()}
    assert {"mybot", "example-openai"} <= names


def test_explicit_env_dir_still_wins(two_dirs, tmp_path, monkeypatch):
    """$ASCEND_CONFIG_DIR is an override and must keep beating both cwd and home."""
    envdir = tmp_path / "envcfg"
    envdir.mkdir()
    (envdir / "mybot.json").write_text('{"adapter": "direct_api", "x": 7}')
    monkeypatch.setenv("ASCEND_CONFIG_DIR", str(envdir))
    assert C.load_config("mybot")["x"] == 7
    assert C.config_dir() == envdir                          # writes still land there


def test_writes_still_target_a_single_directory(two_dirs):
    """config_dir() is the WRITE path and must stay the first existing dir (cwd/configs here)."""
    work, _ = two_dirs
    assert C.config_dir() == work / "configs"


def test_missing_config_error_names_where_it_looked(two_dirs):
    with pytest.raises(FileNotFoundError) as e:
        C.load_config("nope")
    msg = str(e.value)
    assert "nope" in msg
    assert ".ascend" in msg          # the home location must appear, or the hint misleads

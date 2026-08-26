"""Regression tests for config path resolution.

Reported from real use: `discover --out mybot.json` wrote to the CURRENT directory,
but `adapter validate --config mybot` only looked in <repo>/configs, so the file the
tool had just written could not be loaded back. Passing the filename instead
(`--config mybot.json`) produced "configs/mybot.json.json".
"""
import sys, os, json, importlib.util
from pathlib import Path
import pytest

_CLI = Path(__file__).resolve().parent.parent / "shells" / "cli" / "ascend.py"
_spec = importlib.util.spec_from_file_location("ascend_cli", _CLI)
cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli)


@pytest.fixture
def cfgdir(tmp_path, monkeypatch):
    d = tmp_path / "configs"
    d.mkdir()
    monkeypatch.setenv("ASCENDBRIDGE_CONFIG_DIR", str(d))
    return d


def _write(p, obj=None):
    p.write_text(json.dumps(obj or {"adapter": "direct_api", "endpoint": "https://x/y"}))
    return p


def test_bare_name_in_config_dir(cfgdir):
    _write(cfgdir / "bot.json")
    assert cli.resolve_config_path("bot") == cfgdir / "bot.json"


def test_name_with_json_extension_does_not_double_up(cfgdir):
    """REGRESSION: produced 'bot.json.json' and reported it as missing."""
    _write(cfgdir / "bot.json")
    assert cli.resolve_config_path("bot.json") == cfgdir / "bot.json"


def test_file_written_to_cwd_is_found(tmp_path, cfgdir, monkeypatch):
    """REGRESSION: `discover --out bot.json` wrote to cwd; --config could not see it."""
    monkeypatch.chdir(tmp_path)
    _write(tmp_path / "bot.json")
    assert cli.resolve_config_path("bot") == tmp_path / "bot.json"
    assert cli.resolve_config_path("bot.json") == tmp_path / "bot.json"


def test_explicit_relative_and_absolute_paths(tmp_path, cfgdir, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sub = tmp_path / "out"; sub.mkdir()
    _write(sub / "bot.json")
    assert cli.resolve_config_path("out/bot.json") == sub / "bot.json"
    assert cli.resolve_config_path(str(sub / "bot.json")) == sub / "bot.json"


def test_config_dir_wins_when_both_exist(tmp_path, cfgdir, monkeypatch):
    """An explicit path is honoured first; a bare name prefers cwd, then the config dir."""
    monkeypatch.chdir(tmp_path)
    _write(cfgdir / "bot.json", {"adapter": "direct_api", "where": "configdir"})
    assert json.loads(cli.resolve_config_path("bot").read_text())["where"] == "configdir"


def test_missing_config_returns_none(cfgdir):
    assert cli.resolve_config_path("nope") is None
    assert cli.resolve_config_path("") is None
    assert cli.resolve_config_path(None) is None


def test_load_named_config_error_lists_where_it_looked(cfgdir, capsys):
    with pytest.raises(SystemExit):
        cli._load_named_config("nope")
    err = capsys.readouterr().err
    assert "looked in" in err and "ascend adapter configs" in err


def test_load_named_config_reports_bad_json(cfgdir):
    (cfgdir / "broken.json").write_text("{not json")
    with pytest.raises(SystemExit):
        cli._load_named_config("broken")


# --- P0.2: the RELAY (dispatch.load_config) must resolve identically to the CLI ---

def test_dispatch_load_config_matches_cli_resolution(tmp_path, monkeypatch):
    """REGRESSION: `runtime start --config out/bot.json` failed while
    `adapter validate --config out/bot.json` worked, because dispatch.load_config only
    tried <config_dir>/<name>.json. Both now share runtime/configs.resolve_config_path."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "runtime"))
    import dispatch
    monkeypatch.chdir(tmp_path)
    sub = tmp_path / "out"; sub.mkdir()
    (sub / "bot.json").write_text(json.dumps({"adapter": "direct_api", "endpoint": "https://x/y"}))
    for ref in ("out/bot.json", "out/bot", str(sub / "bot.json")):
        cfg = dispatch.load_config(ref)
        assert cfg["endpoint"] == "https://x/y", f"relay failed to load {ref}"


def test_dispatch_load_config_no_double_json(tmp_path, monkeypatch):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "runtime"))
    import dispatch
    d = tmp_path / "configs"; d.mkdir()
    monkeypatch.setenv("ASCENDBRIDGE_CONFIG_DIR", str(d))
    (d / "bot.json").write_text(json.dumps({"adapter": "direct_api"}))
    assert dispatch.load_config("bot.json")["adapter"] == "direct_api"   # was bot.json.json


def test_dispatch_missing_config_raises_configerror(tmp_path, monkeypatch):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "runtime"))
    import dispatch
    monkeypatch.setenv("ASCENDBRIDGE_CONFIG_DIR", str(tmp_path))
    with pytest.raises(dispatch.ConfigError):
        dispatch.load_config("nope")

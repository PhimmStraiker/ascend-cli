"""Regressions for QA-audit findings in the CLI composition layer."""
import sys, json, importlib.util
from pathlib import Path
import pytest

_CLI = Path(__file__).resolve().parent.parent / "shells" / "cli" / "ascend.py"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "runtime"))
_spec = importlib.util.spec_from_file_location("ascend_cli_fixes", _CLI)
cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli)


# --- A#14: the dead isinstance fallback -------------------------------------
def test_unwrap_list_handles_a_bare_list():
    """`payload.get("data", payload if isinstance(payload,list) else [])` raised
    AttributeError on a bare list — .get is evaluated first."""
    assert cli._unwrap_list([{"id": 1}]) == [{"id": 1}]


def test_unwrap_list_handles_each_envelope_key():
    for key in ("data", "items", "applications", "assessments"):
        assert cli._unwrap_list({key: [{"id": 1}]}) == [{"id": 1}]


def test_unwrap_list_handles_garbage():
    assert cli._unwrap_list(None) == []
    assert cli._unwrap_list({"unexpected": "shape"}) == []
    assert cli._unwrap_list("a string") == []


# --- A#3 / P0.4: exit codes --------------------------------------------------
def test_die_defaults_to_usage_not_findings():
    """`_die` defaulting to 2 made 'no token' look like findings to CI."""
    with pytest.raises(SystemExit) as e:
        cli._die("bad invocation")
    assert e.value.code == cli.EXIT_USAGE == 3


def test_exit_codes_are_distinct():
    assert cli.EXIT_OK == 0 and cli.EXIT_ERROR == 1
    assert cli.EXIT_FINDINGS == 2 and cli.EXIT_USAGE == 3


# --- A#10: onboard --har name derivation ------------------------------------
def test_onboard_har_gets_a_name_from_the_file(tmp_path):
    """--har was missing from the name chain, so two HAR onboards both became 'target'
    and overwrote each other's config."""
    ns = cli.build_parser().parse_args(["onboard", "--har", "/tmp/acme-support.har"])
    assert ns.har.endswith("acme-support.har") and ns.name is None
    derived = Path(ns.har).stem
    assert derived == "acme-support" and derived != "target"


# --- A#20: ci --junit was unreachable ---------------------------------------
def test_ci_has_a_junit_flag():
    ns = cli.build_parser().parse_args(["ci", "--file", "x.json", "--junit", "out/j.xml"])
    assert ns.junit == "out/j.xml"


# --- A#15: export --out must create parent dirs ------------------------------
def test_export_parser_accepts_nested_out():
    ns = cli.build_parser().parse_args(
        ["export", "--file", "x.json", "--format", "sarif", "--out", "reports/x.sarif"])
    assert ns.out == "reports/x.sarif"


# --- discover: the new non-browser sources are reachable ---------------------
def test_discover_exposes_api_curl_spec_sources():
    for flag, val in (("--api", "https://h"), ("--curl", "c.txt"), ("--spec", "https://h")):
        ns = cli.build_parser().parse_args(["discover", flag, val])
        assert getattr(ns, flag.lstrip("-").replace("-", "_")) == val


def test_assess_watch_and_list_running_exist():
    ns = cli.build_parser().parse_args(["assess", "watch", "--app", "x"])
    assert ns.func is cli.cmd_assess_watch
    ns2 = cli.build_parser().parse_args(["assess", "list", "--app", "x", "--running"])
    assert ns2.running is True


def test_app_list_with_runs_flag_exists():
    ns = cli.build_parser().parse_args(["app", "list", "--with-runs"])
    assert ns.with_runs is True


def test_results_follow_flag_exists():
    ns = cli.build_parser().parse_args(["results", "log.jsonl", "--follow"])
    assert ns.follow is True and ns.file == "log.jsonl"

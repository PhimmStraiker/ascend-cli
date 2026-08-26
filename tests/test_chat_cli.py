"""Tests for `ascend chat` — the manual/live session command.

The QA audit found manual.py and cmd_chat had ZERO tests, and building the REPL
immediately surfaced two real bugs (a wrong argparse dest, and helper functions that
never made it into the file). Both are pinned here.
"""
import sys, os, json, importlib.util
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "runtime"))
_CLI = Path(__file__).resolve().parent.parent / "shells" / "cli" / "ascend.py"
_spec = importlib.util.spec_from_file_location("ascend_cli", _CLI)
cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli)
import manual


def test_cli_module_has_the_helpers_chat_and_discover_need():
    """REGRESSION: _kv_headers/_finish_discovery were referenced but never defined, so
    `chat <url>` and `discover --api` both died with NameError at runtime."""
    for fn in ("_kv_headers", "_finish_discovery", "_resolve_chat_target", "cmd_chat"):
        assert hasattr(cli, fn), f"{fn} missing from the CLI module"


def test_kv_headers_parses_repeated_headers():
    got = cli._kv_headers(["Authorization: Bearer x", "X-Trace: 1"])
    assert got == {"Authorization": "Bearer x", "X-Trace": "1"}


def test_kv_headers_rejects_malformed():
    with pytest.raises(SystemExit):
        cli._kv_headers(["not-a-header"])


def test_chat_parser_prompt_dest_is_prompts():
    """REGRESSION: _resolve_chat_target read args.prompt while the flag's dest is
    'prompts' (it is repeatable), so `chat <url>` raised AttributeError."""
    ns = cli.build_parser().parse_args(["chat", "mybot", "--prompt", "a", "--prompt", "b"])
    assert ns.prompts == ["a", "b"]
    assert not hasattr(ns, "prompt") or ns.prompts == ["a", "b"]


def test_chat_target_is_positional_and_optional():
    ns = cli.build_parser().parse_args(["chat", "mybot"])
    assert ns.target == "mybot"
    ns2 = cli.build_parser().parse_args(["chat"])
    assert ns2.target is None          # so we can print a helpful error


def test_run_turn_uses_prompt_field_from_config(monkeypatch):
    """REGRESSION (P0.5): the body key was hardcoded to 'prompt', so every turn failed
    on a config that sets prompt_field."""
    seen = {}
    class FakeCaller:
        config = {"prompt_field": "message"}
        adapter_type = "direct_api"
        config_name = "x"
        def handler(self, msg):
            seen.update(msg["payload"]["body"])
            return 200, {"response": "ok"}
    rec = manual.run_turn(FakeCaller(), "hello")
    assert "message" in seen and seen["message"] == "hello"
    assert rec["ok"] is True


def test_load_prompts_reads_plain_and_jsonl(tmp_path):
    f = tmp_path / "p.txt"
    f.write_text("# a comment\nplain one\n"
                 '{"prompt":"with meta","id":"m1","category":"c","expect":"ok"}\n\n')
    items = manual.load_prompts(str(f))
    assert [i["prompt"] for i in items] == ["plain one", "with meta"]
    assert items[1]["id"] == "m1" and items[1]["expect"] == "ok"


def test_turnlog_is_private_and_appends(tmp_path):
    p = tmp_path / "t.jsonl"
    log = manual.TurnLog(str(p))
    log.write({"kind": "turn", "prompt": "a", "response": "b"})
    log.write({"kind": "turn", "prompt": "c", "response": "d"})
    assert len(p.read_text().strip().splitlines()) == 2
    assert oct(p.stat().st_mode)[-3:] == "600"


def test_turnlog_redacts_sensitive_headers(tmp_path):
    p = tmp_path / "t.jsonl"
    manual.TurnLog(str(p)).write({"kind": "turn", "headers": {"Authorization": "Bearer secret"}})
    assert "Bearer secret" not in p.read_text()
    assert "[REDACTED]" in p.read_text()


def test_summarize_counts_expectations():
    recs = [{"ok": True, "duration_ms": 10, "matched": True},
            {"ok": True, "duration_ms": 20, "matched": False},
            {"ok": False, "duration_ms": 30}]
    st = manual.summarize(recs)
    assert st["turns"] == 3 and st["ok"] == 2 and st["failed"] == 1
    assert st["checked"] == 2 and st["matched"] == 1

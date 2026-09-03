"""
test_custom_adapter_front_door.py — a hand-written adapter must be onboardable from `target`.

The `custom` adapter has always worked at runtime: `{"adapter": "custom", "adapter_module":
"x.py"}` plus a module exposing `def send_prompt(prompt: str) -> str`, run by the bridge in a
worker thread exactly like a built-in. What was missing was a front door. To use it you had to

  1. know the contract existed at all (nothing in `target add --help` mentioned it, and no
     `example-custom_module.json` shipped, while every other adapter had an example),
  2. hand-write the pointer JSON yourself, and
  3. know the config-dir resolution rules, because `--config` took a NAME, not a path — an
     agent that had just written /tmp/thing.json could not point at it.

So the capability existed and the product hid it. That matters most for exactly the targets
derivation cannot reach — a signature computed per request, a per-request nonce, a rotating
conversation id, SOAP framing, a job polled to completion — where "write the adapter" is the
correct answer rather than a failure.

The `adapter_module` path is stored ABSOLUTE unless the module already sits in the config dir,
because that directory and the cwd are the only places `custom_module._resolve_module_path`
looks. A bare filename recorded for a module living anywhere else validates at build time and
then fails at run time with "adapter module not found" — the worst split, since the hard gate
goes green and the probes do not.
"""
import json
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "shells" / "cli"))
sys.path.insert(0, str(REPO / "runtime"))
import ascend  # noqa: E402


class TestScaffold:
    def test_it_writes_a_module_with_the_contract(self, tmp_path):
        p = ascend._write_custom_scaffold(str(tmp_path / "a.py"), "https://bot.example.com/chat")
        src = p.read_text()
        assert "def send_prompt(prompt: str) -> str:" in src, "the stub must carry the contract"
        compile(src, str(p), "exec")          # it must actually be runnable Python

    def test_the_target_url_is_seeded_from_the_source_flag(self, tmp_path):
        p = ascend._write_custom_scaffold(str(tmp_path / "b.py"), "https://bot.example.com/chat")
        assert p.read_text().count("https://bot.example.com/chat") >= 2, \
            "the stub should point at the target so it can run before being edited"

    def test_it_refuses_to_overwrite(self, tmp_path):
        p = tmp_path / "c.py"
        p.write_text("# work I already did\n")
        with pytest.raises(SystemExit):
            ascend._write_custom_scaffold(str(p), None)
        assert p.read_text() == "# work I already did\n", "an edited adapter must survive"

    def test_it_requires_a_py_path(self, tmp_path):
        with pytest.raises(SystemExit):
            ascend._write_custom_scaffold(str(tmp_path / "d.txt"), None)


class TestModuleToConfig:
    def _mod(self, tmp_path, body="def send_prompt(prompt: str) -> str:\n    return 'x'\n"):
        p = tmp_path / "m.py"
        p.write_text(body)
        return p

    def test_it_produces_a_loadable_pointer_config(self, tmp_path):
        cfg = ascend._config_from_module(str(self._mod(tmp_path)))
        assert cfg["adapter"] == "custom"
        assert cfg["adapter_module"].endswith("m.py")
        assert cfg["timeout_ms"] >= 1000

    def test_a_module_outside_the_config_dir_is_recorded_absolute(self, tmp_path):
        """A bare filename here validates and then fails at run time — the worst split."""
        cfg = ascend._config_from_module(str(self._mod(tmp_path)))
        assert os.path.isabs(cfg["adapter_module"]), (
            "a module outside the config dir must be an absolute path, or the adapter that "
            "just passed the hard gate will not load when the probes arrive")

    def test_a_module_without_the_contract_is_refused(self, tmp_path):
        with pytest.raises(SystemExit):
            ascend._config_from_module(str(self._mod(tmp_path, "def nope():\n    pass\n")))

    def test_a_missing_module_is_refused(self, tmp_path):
        with pytest.raises(SystemExit):
            ascend._config_from_module(str(tmp_path / "nothing-here.py"))

    def test_a_non_python_file_is_refused(self, tmp_path):
        p = tmp_path / "x.json"
        p.write_text("{}")
        with pytest.raises(SystemExit):
            ascend._config_from_module(str(p))

    def test_the_timeout_is_configurable(self, tmp_path):
        cfg = ascend._config_from_module(str(self._mod(tmp_path)), timeout_ms=250000)
        assert cfg["timeout_ms"] == 250000, "a bespoke adapter may legitimately be slow"


class TestConfigAcceptsAPath:
    def test_a_json_path_loads_directly(self, tmp_path):
        p = tmp_path / "hand.json"
        p.write_text(json.dumps({"adapter": "custom", "adapter_module": "x.py"}))
        assert ascend._load_named_config(str(p))["adapter"] == "custom"

    def test_a_name_still_resolves_through_the_config_dir(self):
        """The path support must not cost the existing name lookup."""
        cfg = ascend._load_named_config("example-direct_api")
        assert cfg.get("adapter") == "direct_api"

    def test_an_unreadable_path_fails_loudly(self, tmp_path):
        p = tmp_path / "broken.json"
        p.write_text("{not json")
        with pytest.raises(SystemExit):
            ascend._load_named_config(str(p))


class TestItIsDiscoverable:
    """The capability existed before; nobody could find it. That was the actual defect."""

    def test_an_example_config_ships(self):
        cfg = REPO / "configs" / "example-custom_module.json"
        assert cfg.is_file(), "every other adapter ships an example; this one did not"
        d = json.loads(cfg.read_text())
        assert d["adapter"] == "custom" and d["adapter_module"]

    def test_the_example_module_ships_and_compiles(self):
        mod = REPO / "configs" / "example-custom_module.py"
        assert mod.is_file()
        src = mod.read_text()
        assert "def send_prompt(prompt: str) -> str:" in src
        compile(src, str(mod), "exec")

    @pytest.mark.parametrize("flag", ["--module", "--scaffold"])
    def test_the_flags_are_on_target_add(self, flag):
        p = ascend.build_parser()
        target = [a for a in p._actions if getattr(a, "choices", None)][0].choices["target"]
        add = [a for a in target._actions if getattr(a, "choices", None)][0].choices["add"]
        assert flag in {o for a in add._actions for o in (a.option_strings or [])}

    def test_the_build_adapter_skill_tells_an_agent_when_to_write_one(self):
        """Derivation failing must route to 'write the adapter', not to endless iteration."""
        s = (REPO / "skills" / "build-adapter" / "SKILL.md").read_text()
        assert "--scaffold" in s and "--module" in s
        assert "send_prompt" in s

    def test_the_onboard_skill_points_at_the_same_path(self):
        s = (REPO / "skills" / "onboard-target" / "SKILL.md").read_text()
        assert "--module" in s and "--scaffold" in s

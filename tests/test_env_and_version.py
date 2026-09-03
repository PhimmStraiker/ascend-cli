"""
test_env_and_version.py — three defects that were each invisible to the existing suite.

All three share a root cause: nothing in the suite ran the CLI on a TTY or asked what a switch
does when it is set to a FALSY value, so the whole "user opted out" path was unexecuted.

1. `ASCEND_FORCE_COLOR=0` forced colour ON, because every non-empty string is truthy in Python.
   The damaging case is a pipe: someone sets it to 0 meaning "off", and gets ANSI escapes written
   into a file or a log. `ASCEND_PLAIN=0` had the mirror defect on the one switch that exists to
   make a corrupted terminal stop.
2. `Progress._write` padded and erased by `len(line)`, which counts escape BYTES. `_line()`
   wraps the elapsed clock in _DIM/_OFF, so bytes and cells diverge by 8 exactly when the clock
   appears at the 3s mark -- the moment the padding has to be right.
3. `ascend version --json` printed bare text. It was the only command that ignored --json, so an
   agent that asked for JSON got `1.1.1` and a parse error.

NO_COLOR is deliberately excluded from the falsy-value handling: its spec is presence-based, so
`NO_COLOR=0` correctly disables colour. That is asserted here too, so a future "consistency"
cleanup cannot quietly break the convention.
"""
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "runtime"))
import ui  # noqa: E402


class _Stream(io.StringIO):
    encoding = "utf-8"

    def __init__(self, tty):
        super().__init__()
        self._tty = tty

    def isatty(self):
        return self._tty


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("ASCEND_FORCE_COLOR", "ASCEND_PLAIN", "NO_COLOR", "ASCEND_COLOR_DEPTH"):
        monkeypatch.delenv(k, raising=False)


class TestOurSwitchesHonourFalsyValues:
    """`=0` / `=false` / `=no` / `=off` mean off, for the switches we define."""

    @pytest.mark.parametrize("val", ["0", "false", "False", "no", "off", "", "  "])
    def test_force_color_falsy_does_not_force_colour_into_a_pipe(self, monkeypatch, val):
        # The bug: any non-empty value was truthy, so `=0` pushed escapes into a pipe.
        monkeypatch.setenv("ASCEND_FORCE_COLOR", val)
        assert ui.color_ok(_Stream(tty=False)) is False

    @pytest.mark.parametrize("val", ["1", "true", "yes", "on"])
    def test_force_color_truthy_still_forces(self, monkeypatch, val):
        monkeypatch.setenv("ASCEND_FORCE_COLOR", val)
        assert ui.color_ok(_Stream(tty=False)) is True

    @pytest.mark.parametrize("val", ["0", "false", "no", "off", ""])
    def test_plain_falsy_does_not_silence_a_terminal(self, monkeypatch, val):
        """ASCEND_PLAIN=0 must not do the opposite of what it says."""
        monkeypatch.setenv("ASCEND_PLAIN", val)
        assert ui.color_ok(_Stream(tty=True)) is True

    @pytest.mark.parametrize("val", ["1", "true", "yes", "on"])
    def test_plain_truthy_silences_everything(self, monkeypatch, val):
        monkeypatch.setenv("ASCEND_PLAIN", val)
        assert ui.color_ok(_Stream(tty=True)) is False
        assert ui.color_depth(_Stream(tty=True)) == 0

    def test_plain_beats_force_color(self, monkeypatch):
        """The stop-everything hatch outranks the force switch, in both orders of setting."""
        monkeypatch.setenv("ASCEND_FORCE_COLOR", "1")
        monkeypatch.setenv("ASCEND_PLAIN", "1")
        assert ui.color_ok(_Stream(tty=True)) is False


class TestNoColorKeepsItsSpec:
    """no-color.org is presence-based; any non-empty value disables. Not our switch to reinterpret."""

    @pytest.mark.parametrize("val", ["1", "0", "false", "no", "anything"])
    def test_any_non_empty_value_disables(self, monkeypatch, val):
        monkeypatch.setenv("NO_COLOR", val)
        assert ui.color_ok(_Stream(tty=True)) is False

    def test_empty_is_not_an_opt_out(self, monkeypatch):
        monkeypatch.setenv("NO_COLOR", "")
        assert ui.color_ok(_Stream(tty=True)) is True


class TestProgressMeasuresCellsNotBytes:
    def test_width_tracks_visible_cells_once_the_clock_appears(self, monkeypatch):
        """The 3s boundary is where bytes and cells diverge by 8 (the _DIM/_OFF pair)."""
        s = _Stream(tty=True)
        p = ui.Progress("reading", total=5, enabled=True, stream=s)
        p._started = 0.0            # force the elapsed clock to render
        line = p._line("-")
        assert "\033" in line, "no escapes in the line means this test proves nothing"
        assert len(line) > ui.vwidth(line), "expected bytes to exceed cells"
        p._write(line)
        assert p._width == ui.vwidth(line), (
            f"_width tracked {p._width} (bytes={len(line)}, cells={ui.vwidth(line)})")

    def test_erase_clears_exactly_the_cells_written(self):
        """Over-erasing by the escape-byte count can wrap the line and eat scrollback."""
        s = _Stream(tty=True)
        p = ui.Progress("reading", total=5, enabled=True, stream=s)
        p._started = 0.0
        p._write(p._line("-"))
        cells = p._width
        s.truncate(0), s.seek(0)
        p._erase()
        spaces = s.getvalue().count(" ")
        assert spaces == cells, f"erased {spaces} spaces for {cells} visible cells"

    def test_shrinking_line_is_fully_padded_over(self):
        """A wide frame followed by a narrow one must leave no remnant of the wide one."""
        s = _Stream(tty=True)
        p = ui.Progress("a-very-long-phase-name-here", enabled=True, stream=s)
        p._started = 0.0
        p._write(p._line("-"))
        wide = p._width
        p.set_phase("short")
        s.truncate(0), s.seek(0)
        narrow_line = p._line("-")
        p._write(narrow_line)
        written = s.getvalue()
        assert ui.vwidth(written.lstrip("\r")) >= wide, (
            "the narrow frame plus its padding must cover the wide frame it replaced")


class TestVersionHonoursJson:
    def _run(self, *argv):
        env = {k: v for k, v in os.environ.items() if k != "ASCEND_FORCE_COLOR"}
        env.update({"NO_COLOR": "1", "TERM": "dumb", "ASCEND_NO_SPINNER": "1"})
        return subprocess.run([sys.executable, str(REPO / "shells" / "cli" / "ascend.py"), *argv],
                              capture_output=True, text=True, cwd=str(REPO), env=env, timeout=60)

    def test_version_subcommand_json_parses(self):
        r = self._run("version", "--json")
        assert r.returncode == 0
        assert json.loads(r.stdout)["version"], f"not JSON: {r.stdout!r}"

    def test_version_flag_json_parses(self):
        r = self._run("--version", "--json")
        assert r.returncode == 0
        assert json.loads(r.stdout)["version"], f"not JSON: {r.stdout!r}"

    def test_the_two_forms_agree(self):
        assert (json.loads(self._run("version", "--json").stdout)
                == json.loads(self._run("--version", "--json").stdout))

    def test_human_form_is_still_a_bare_version(self):
        """Scripts do `ascend version` and compare the string; tests/backcompat freezes it too."""
        r = self._run("version")
        assert r.returncode == 0
        assert r.stdout.strip().count("\n") == 0
        assert not r.stdout.lstrip().startswith("{"), "the human form must not become JSON"

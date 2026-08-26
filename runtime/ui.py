"""
ui.py — terminal presentation: progress that tells the truth, and color that never lies.

Two rules everything here follows:

1. **Never corrupt output.** Progress goes to stderr, is erased before anything real prints, and
   turns itself off when stdout is not a TTY, when `--json` is in play, or when `NO_COLOR` /
   `ASCEND_NO_SPINNER` are set. A piped or agent-driven run sees byte-identical output to before.
2. **Never invent progress.** The phase text and counts come from real work completing. A spinner
   that keeps twirling while nothing happens is worse than no spinner, so `Progress.advance()` is
   called by the code that actually finished something.

Why it exists: the CLI fans out one API call per app (there is no tenant-wide endpoint), so a
40-app tenant means 40 round-trips. That was ~9s of blank screen. Same work, but now you can see
it happening — which is most of what "feels fast" actually means.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from typing import Optional

_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"          # braille: 1 cell wide, reads as motion even at 10fps
_ASCII_FRAMES = "|/-\\"            # fallback for terminals that mangle unicode

# ANSI, applied only when color_ok()
_DIM = "\033[2m"
_OFF = "\033[0m"
_BOLD = "\033[1m"
_RED = "\033[31m"
_YEL = "\033[33m"
_GRN = "\033[32m"
_CYA = "\033[36m"
_MAG = "\033[35m"

_SEV_COLOR = {
    "critical": _MAG, "high": _RED, "medium": _YEL,
    "low": _GRN, "info": _CYA, "informational": _CYA, "none": _DIM, "unknown": _RED,
}


def color_ok(stream=None) -> bool:
    """True when it is safe to emit ANSI: a real TTY, and the user has not opted out."""
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("ASCEND_FORCE_COLOR"):
        return True
    s = stream or sys.stdout
    try:
        return bool(s.isatty())
    except Exception:
        return False


def _unicode_ok() -> bool:
    enc = (getattr(sys.stderr, "encoding", "") or "").lower()
    return "utf" in enc


def paint(text: str, color: str, stream=None) -> str:
    return f"{color}{text}{_OFF}" if color_ok(stream) else text


def severity_chip(sev: Optional[str], width: int = 8) -> str:
    """A severity rendered as a fixed-width, colored cell. Padding happens OUTSIDE the escape
    codes so column alignment survives when color is off."""
    s = (str(sev) if sev else "-").lower()
    cell = s[:width].ljust(width)
    return paint(cell, _SEV_COLOR.get(s, ""), sys.stdout)


def risk_dot(sev: Optional[str]) -> str:
    """A single colored ● read at a glance: magenta critical, red high, yellow medium, green low."""
    s = (str(sev) if sev else "").lower()
    glyph = "●" if _unicode_ok() else "*"      # ● / *
    return paint(glyph, _SEV_COLOR.get(s, _DIM), sys.stdout)


def score_cell(v, width: int = 5) -> str:
    """Right-aligned, color-banded score (the platform's failure score: 0-100, higher = worse).
    Red >=67, yellow >=34, green below — padding OUTSIDE the escape so columns stay aligned."""
    if not isinstance(v, (int, float)):
        return "-".rjust(width)
    txt = f"{float(v):.0f}" if float(v) == int(float(v)) else f"{float(v):.1f}"
    col = _RED if v >= 67 else (_YEL if v >= 34 else _GRN)
    return paint(txt.rjust(width), col, sys.stdout)


def bar(passed: int, failed: int, width: int = 10) -> str:
    """`▓▓▓▓▓▓▓░░░` — pass/fail at a glance. ASCII when the terminal can't do blocks."""
    total = max(0, passed) + max(0, failed)
    if not total:
        return "-".ljust(width)
    filled = int(round(width * (max(0, passed) / total)))
    if _unicode_ok():
        good, bad = "▓", "░"
    else:
        good, bad = "#", "."
    return paint(good * filled, _GRN, sys.stdout) + paint(bad * (width - filled), _RED, sys.stdout)


def sparkline(values) -> str:
    """Tiny trend of scores over runs, so a regression is visible without reading numbers."""
    nums = [float(v) for v in values if isinstance(v, (int, float))]
    if not nums:
        return ""
    if not _unicode_ok():
        return ",".join(f"{n:.0f}" for n in nums[-8:])
    blocks = "▁▂▃▄▅▆▇█"
    lo, hi = min(nums), max(nums)
    span = (hi - lo) or 1.0
    return "".join(blocks[min(len(blocks) - 1, int((n - lo) / span * (len(blocks) - 1)))]
                   for n in nums[-16:])


class Progress:
    """A spinner with a live phase and an optional count.

        with Progress("reading assessments", total=39) as p:
            ...
            p.advance()          # called when one unit of real work finished

    Disabled (a no-op with zero output) when not interactive, under --json, or when the user set
    NO_COLOR / ASCEND_NO_SPINNER. Always erases its line before returning so real output is clean.
    """

    def __init__(self, phase: str, total: Optional[int] = None, *, enabled: Optional[bool] = None,
                 stream=None, interval: float = 0.1):
        self.stream = stream or sys.stderr
        self.phase = phase
        self.total = total
        self.done = 0
        self.interval = interval
        if enabled is None:
            enabled = (color_ok(self.stream)
                       and not os.environ.get("ASCEND_NO_SPINNER"))
        self.enabled = bool(enabled)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._started = 0.0
        self._width = 0

    # -- lifecycle ------------------------------------------------------------
    def __enter__(self) -> "Progress":
        self.start()
        return self

    def __exit__(self, *_exc) -> None:
        self.stop()

    def start(self) -> None:
        if not self.enabled or self._thread:
            return
        self._started = time.time()
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=0.5)
            self._thread = None
        self._erase()

    # -- updates --------------------------------------------------------------
    def advance(self, n: int = 1) -> None:
        with self._lock:
            self.done += n

    def set_phase(self, phase: str, total: Optional[int] = None) -> None:
        with self._lock:
            self.phase = phase
            if total is not None:
                self.total = total
                self.done = 0

    # -- rendering ------------------------------------------------------------
    def _line(self, frame: str) -> str:
        with self._lock:
            phase, done, total = self.phase, self.done, self.total
        count = f"  {done}/{total}" if total else (f"  {done}" if done else "")
        elapsed = time.time() - self._started
        secs = f"  {elapsed:.0f}s" if elapsed >= 3 else ""
        return f"  {frame} {phase}{count}{_DIM}{secs}{_OFF}"

    def _spin(self) -> None:
        frames = _FRAMES if _unicode_ok() else _ASCII_FRAMES
        i = 0
        while not self._stop.is_set():
            line = self._line(frames[i % len(frames)])
            self._write(line)
            i += 1
            self._stop.wait(self.interval)

    def _write(self, line: str) -> None:
        try:
            pad = max(0, self._width - len(line))
            self.stream.write("\r" + line + " " * pad)
            self.stream.flush()
            self._width = len(line)
        except Exception:
            self.enabled = False

    def _erase(self) -> None:
        if not self.enabled or not self._width:
            return
        try:
            self.stream.write("\r" + " " * self._width + "\r")
            self.stream.flush()
        except Exception:
            pass
        self._width = 0


def progress(phase: str, total: Optional[int] = None, *, args=None, **kw) -> Progress:
    """Progress bound to the CLI's conventions: silent whenever --json is set."""
    enabled = kw.pop("enabled", None)
    if enabled is None and args is not None and getattr(args, "json", False):
        enabled = False
    return Progress(phase, total, enabled=enabled, **kw)

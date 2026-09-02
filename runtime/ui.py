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


# ===========================================================================================
# Presentation primitives — brand colour, visible width, panels, and a progress bar.
#
# Everything below obeys the two rules at the top of this module, plus three of its own:
#
#   a. ONE colour gate. `color_depth()` folds every reason not to emit escapes into a single
#      integer (0 | 8 | 256 | 24). Renderers branch on that and nothing else, which is what
#      makes four-tier degradation testable: monkeypatch one function, assert four outputs.
#   b. GEOMETRY BEFORE COLOUR. Widths are computed in visible cells before a colour is chosen,
#      so every tier renders the same number of columns by construction rather than because
#      four code paths happen to agree.
#   c. NEVER RAISE. These are decorations on someone else's output. A renderer that throws on an
#      odd locale would turn a cosmetic feature into an outage, so each one falls back to plain
#      text. `Progress._write` already sets the precedent.
# ===========================================================================================

import re as _re
import shutil as _shutil
import unicodedata as _ud

# The brand ramp, taken from docs/architecture.html so the terminal and the docs cannot drift.
# Three different pinks used to coexist in this repo (38;5;205, 38;5;204, #FF5378); this is the
# one definition.
BRAND = {
    "ascend":   (255, 83, 120),    # #FF5378 — Ascend red/pink, the primary accent
    "pink":     (240, 109, 154),   # #F06D9A
    "gold":     (200, 138, 30),    # #C88A1E
    "gold_lt":  (255, 201, 113),   # #FFC971 — the light end of the progress ramp
    "defend":   (45, 198, 255),    # #2DC6FF
    "unveil":   (192, 132, 252),   # #C084FC
    "glint":    (255, 190, 210),
}
GRADIENT_BAR = ("gold_lt", "ascend")     # #FFC971 -> #FF5378, warm to brand

# Colour for a state word. A whitelist on purpose: an unrecognised word passes through
# untouched rather than being guessed at, so a new platform status can never come out
# mis-coloured (green "failed" is worse than uncoloured "failed").
STATE_TONE = {
    "serving": "ok", "running": "ok", "complete": "ok", "yes": "ok", "ok": "ok",
    "pass": "ok", "up_to_date": "ok",
    "paused": "warn", "queued": "warn", "created": "warn", "pending": "warn",
    "dead": "alarm", "failed": "alarm", "error": "alarm", "gone": "alarm",
    "fail": "alarm", "none": "dim", "-": "dim", "not": "dim", "unknown": "dim",
}
_TONE_8 = {"ok": _GRN, "warn": _YEL, "alarm": _RED, "info": _CYA, "dim": _DIM, "": ""}
_TONE_RGB = {"ok": (120, 220, 150), "warn": BRAND["gold_lt"], "alarm": BRAND["ascend"],
             "info": BRAND["defend"], "dim": None}

_ANSI_RE = _re.compile(r"\x1b(?:\[[0-9;?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")


# ---- capability gates ---------------------------------------------------------------------
def json_mode() -> bool:
    """True when `--json` is on the command line.

    Mirrors `_wants_json()` in the CLI, which reads argv directly because errors can be raised
    before argparse runs. Colour has 48 separate `args.json` checks upstream of it; folding this
    into `color_depth` means even a mistaken paint call under `--json` emits plain bytes.
    """
    try:
        return "--json" in sys.argv
    except Exception:
        return False


def truecolor_ok() -> bool:
    """24-bit colour support, by declaration or by known-good terminal."""
    try:
        if (os.environ.get("COLORTERM") or "").lower() in ("truecolor", "24bit"):
            return True
        return (os.environ.get("TERM_PROGRAM") or "") in (
            "iTerm.app", "WezTerm", "ghostty", "vscode", "Apple_Terminal")
    except Exception:
        return False


def color_depth(stream=None) -> int:
    """How much colour this stream may carry: 0 (none) | 8 | 256 | 24.

    The single gate. Ordered so the loudest opt-out wins:
      ASCEND_PLAIN > NO_COLOR > --json (stdout only) > TERM=dumb > ASCEND_COLOR_DEPTH > detection
    """
    try:
        if os.environ.get("ASCEND_PLAIN"):
            return 0
        s = stream if stream is not None else sys.stdout
        # `--json` silences stdout only. Progress and notes live on stderr and stay readable.
        if json_mode() and s is sys.stdout:
            return 0
        if not color_ok(s):
            return 0
        if (os.environ.get("TERM") or "").lower() in ("dumb", "unknown"):
            return 0
        forced = os.environ.get("ASCEND_COLOR_DEPTH")
        if forced:
            want = {"0": 0, "1": 0, "8": 8, "16": 8, "256": 256, "24": 24, "24bit": 24}
            return want.get(forced.strip().lower(), 256)
        if truecolor_ok():
            return 24
        if "256" in (os.environ.get("TERM") or ""):
            return 256
        return 256 if os.environ.get("TERM") else 8
    except Exception:
        return 0


def unicode_ok(stream=None) -> bool:
    """Whether this stream can carry block/box glyphs.

    `_unicode_ok()` inspects stderr only, which is wrong for stdout renderers but is relied on by
    the existing helpers; this is the stream-aware form, defaulting to the old behaviour.
    """
    try:
        s = stream if stream is not None else sys.stderr
        enc = (getattr(s, "encoding", "") or "").lower()
        return "utf" in enc
    except Exception:
        return False


# ---- colour conversion --------------------------------------------------------------------
_XT_STEPS = (0, 95, 135, 175, 215, 255)


def rgb256(r: int, g: int, b: int) -> int:
    """Nearest xterm-256 cube index for an RGB triple."""
    def _ix(v):
        return min(range(6), key=lambda i: abs(_XT_STEPS[i] - max(0, min(255, int(v)))))
    return 16 + 36 * _ix(r) + 6 * _ix(g) + _ix(b)


def brand(name: str):
    return BRAND.get(name, BRAND["ascend"])


def rich(depth: int) -> bool:
    """Whether this depth can carry a per-cell RGB ramp (256-colour or truecolour).

    Depth is NOT an ordered scale: 24 means bit depth, 256 means colour count, so `24 < 256`
    is arithmetically true and semantically backwards. Every decision uses explicit membership.
    """
    return depth in (256, 24)


def sgr(rgb, *, layer: int = 38, depth: Optional[int] = None, stream=None) -> str:
    """An RGB triple as an escape at the right depth. The ONE place that conversion happens."""
    try:
        d = color_depth(stream) if depth is None else depth
        if not d:
            return ""
        r, g, b = rgb
        if d == 24:
            return f"\033[{layer};2;{int(r)};{int(g)};{int(b)}m"
        if d == 256:
            return f"\033[{layer};5;{rgb256(r, g, b)}m"
        # 8-colour has no brand hue; approximate by luminance-free nearest primary.
        return _RED if r >= g and r >= b else (_GRN if g >= b else _CYA)
    except Exception:
        return ""


# ---- measurement --------------------------------------------------------------------------
def _dim(text: str, depth: int) -> str:
    """Dim `text` honouring an EXPLICIT depth. `paint()` re-checks color_ok and therefore
    discards a caller-supplied depth, which made every tier render as plain."""
    return f"{_DIM}{text}{_OFF}" if depth else text


def strip_ansi(s: str) -> str:
    """`s` with every escape sequence removed."""
    try:
        return _ANSI_RE.sub("", str(s))
    except Exception:
        return str(s)


def vwidth(s: str) -> int:
    """Visible width in terminal cells: escapes are free, combining marks are zero, CJK is two."""
    try:
        w = 0
        for ch in strip_ansi(s):
            if _ud.combining(ch):
                continue
            w += 2 if _ud.east_asian_width(ch) in ("W", "F") else 1
        return w
    except Exception:
        return len(strip_ansi(s))


def vpad(s: str, width: int, *, align: str = "left") -> str:
    """Pad to a VISIBLE width. `f"{s:10}"` counts escape bytes and silently under-pads."""
    try:
        gap = max(0, int(width) - vwidth(s))
        if align == "right":
            return " " * gap + s
        if align == "center":
            left = gap // 2
            return " " * left + s + " " * (gap - left)
        return s + " " * gap
    except Exception:
        return s


def vtrunc(s: str, width: int, *, ellipsis: str = "…") -> str:
    """Truncate to a visible width without ever splitting an escape sequence."""
    try:
        if vwidth(s) <= width:
            return s
        ell = ellipsis if unicode_ok(sys.stdout) else "..."
        budget = max(0, int(width) - vwidth(ell))
        out, seen, i, raw = [], 0, 0, str(s)
        while i < len(raw):
            m = _ANSI_RE.match(raw, i)
            if m:                                  # escapes cost nothing and are never cut
                out.append(m.group(0))
                i = m.end()
                continue
            ch = raw[i]
            cw = 0 if _ud.combining(ch) else (2 if _ud.east_asian_width(ch) in ("W", "F") else 1)
            if seen + cw > budget:
                break
            out.append(ch)
            seen += cw
            i += 1
        return "".join(out) + (_OFF if "\033" in raw else "") + ell
    except Exception:
        return str(s)[:max(0, width)]


def term_width(stream=None, *, default: int = 100, minimum: int = 40, maximum: int = 120) -> int:
    """Usable width, clamped.

    Returns `default` when the stream is not a TTY. That is deliberate: if box widths tracked the
    real terminal size on a pipe, every subprocess test would become width-dependent and
    irreproducible across machines and CI.
    """
    try:
        s = stream if stream is not None else sys.stdout
        if not s.isatty():
            return default
        cols = _shutil.get_terminal_size(fallback=(default, 24)).columns
        return max(minimum, min(maximum, int(cols)))
    except Exception:
        return default


# ---- structure ----------------------------------------------------------------------------
def _glyphs(stream):
    if unicode_ok(stream):
        return {"h": "─", "v": "│", "tl": "╭", "tr": "╮", "bl": "╰", "br": "╯",
                "full": "█", "void": "░", "warn": "▲", "dot": "·"}
    return {"h": "-", "v": "|", "tl": "+", "tr": "+", "bl": "+", "br": "+",
            "full": "#", "void": ".", "warn": "!", "dot": "-"}


def rule(width: Optional[int] = None, *, stream=None, indent: str = "  ") -> str:
    g = _glyphs(stream)
    w = width if width is not None else term_width(stream) - len(indent)
    return indent + g["h"] * max(1, w)


def section(label: str, *, stream=None, indent: str = "  ") -> str:
    """A quiet, dim, upper-case section label — the existing house style, just styled."""
    d = color_depth(stream)
    txt = str(label).upper()
    return indent + (f"{_DIM}{txt}{_OFF}" if d else txt)


def state(word, *, width: int = 0, stream=None) -> str:
    """A state word, coloured by meaning, padded to a VISIBLE width.

    Unknown words pass through unchanged — see STATE_TONE.
    """
    try:
        raw = "-" if word is None else str(word)
        tone = STATE_TONE.get(raw.strip().lower().lstrip("*!"), "")
        d = color_depth(stream)
        if not d or not tone:
            return vpad(raw, width) if width else raw
        rgbv = _TONE_RGB.get(tone)
        pre = (sgr(rgbv, depth=d, stream=stream) if rgbv and rich(d) else _TONE_8.get(tone, ""))
        return vpad(f"{pre}{raw}{_OFF}", width) if width else f"{pre}{raw}{_OFF}"
    except Exception:
        return vpad(str(word), width) if width else str(word)


def header(title: str, *, subtitle: str = "", accent: str = "ascend",
           width: Optional[int] = None, stream=None, indent: str = "  ") -> str:
    """A boxed command header: the wordmark letter-spaced, the command path verbatim.

    Only the wordmark is letter-spaced. Letter-spacing the command path would make it
    unsearchable and unreadable, and would break anything grepping for `assess run`.
    """
    try:
        g, d = _glyphs(stream), color_depth(stream)
        spaced = " ".join(str(title).upper())
        acc = sgr(brand(accent), depth=d, stream=stream) if d else ""
        body = (f"{acc}{spaced}{_OFF}" if d else spaced)
        if subtitle:
            sep = f"{_DIM} {g['dot']} {_OFF}" if d else f" {g['dot']} "
            body += sep + (f"{_DIM}{subtitle}{_OFF}" if d else subtitle)
        inner = vwidth(body) + 2
        w = min(inner, (width if width is not None else term_width(stream)) - len(indent) - 2)
        line = f"{indent}{g['tl']}{g['h'] * w}{g['tr']}\n"
        line += f"{indent}{g['v']} {vpad(body, max(0, w - 2))} {g['v']}\n"
        line += f"{indent}{g['bl']}{g['h'] * w}{g['br']}"
        return line
    except Exception:
        return f"{indent}{title}" + (f" - {subtitle}" if subtitle else "")


def panel(lines, *, title: str = "", tone: str = "info", hint: str = "",
          width: Optional[int] = None, stream=None, indent: str = "  ") -> str:
    """A bordered block for something the operator must not scroll past.

    Wraps by VISIBLE width and clamps to the terminal, so a long control id cannot produce a
    ragged box that looks worse than no box at all.
    """
    try:
        g, d = _glyphs(stream), color_depth(stream)
        rgbv = _TONE_RGB.get(tone)
        acc = (sgr(rgbv, depth=d, stream=stream) if rgbv and rich(d) else _TONE_8.get(tone, "")) if d else ""
        avail = (width if width is not None else term_width(stream)) - len(indent) - 4
        body = []
        for ln in ([lines] if isinstance(lines, str) else list(lines)):
            for chunk in _wrap_visible(str(ln), avail):
                body.append(chunk)
        inner = max([vwidth(b) for b in body] + [vwidth(title) + 2, vwidth(hint)] + [1])
        inner = min(inner, avail)
        head = f"{g['h'] * 1} {acc}{title}{_OFF} " if (title and d) else (f"{g['h']} {title} " if title else "")
        top = f"{indent}{g['tl']}{head}{g['h'] * max(0, inner + 2 - vwidth(head))}{g['tr']}"
        out = [top]
        for b in body:
            out.append(f"{indent}{g['v']} {vpad(b, inner)} {g['v']}")
        if hint:
            h = f"{_DIM}{hint}{_OFF}" if d else hint
            out.append(f"{indent}{g['v']} {vpad(h, inner)} {g['v']}")
        out.append(f"{indent}{g['bl']}{g['h'] * (inner + 2)}{g['br']}")
        return "\n".join(out)
    except Exception:
        pre = f"{indent}{title}: " if title else indent
        return pre + " ".join(str(x) for x in ([lines] if isinstance(lines, str) else lines))


def _wrap_visible(s: str, width: int):
    """Word-wrap by visible width. Escapes ride along with the word they are attached to."""
    if width <= 0 or vwidth(s) <= width:
        return [s]
    out, cur = [], ""
    for word in str(s).split(" "):
        cand = word if not cur else f"{cur} {word}"
        if vwidth(cand) <= width:
            cur = cand
        else:
            if cur:
                out.append(cur)
            cur = word if vwidth(word) <= width else vtrunc(word, width)
    if cur:
        out.append(cur)
    return out or [s]


def kv(pairs, *, label_width: Optional[int] = None, stream=None,
       indent: str = "  ", gap: int = 2):
    """An aligned key/value block: dim labels, one column, computed not hand-typed.

    Returns a list of lines. Skips pairs whose value is None, matching how these blocks are
    already built by hand.
    """
    try:
        items = [(str(k), v) for k, v in (pairs.items() if hasattr(pairs, "items") else pairs)
                 if v is not None]
        if not items:
            return []
        d = color_depth(stream)
        lw = label_width if label_width is not None else max(len(k) for k, _ in items)
        out = []
        for k, v in items:
            label = vpad(f"{_DIM}{k}{_OFF}" if d else k, lw)
            out.append(f"{indent}{label}{' ' * gap}{v}")
        return out
    except Exception:
        return [f"{indent}{k}  {v}" for k, v in
                (pairs.items() if hasattr(pairs, "items") else pairs) if v is not None]


# ---- the progress bar ---------------------------------------------------------------------
def gradient_bar(frac, *, width: int = 24, ramp=GRADIENT_BAR, label: str = "",
                 eta: str = "", depth: Optional[int] = None, stream=None) -> str:
    """A progress bar whose visible width is identical at every colour depth.

    Geometry is decided first, in cells, and colour is then painted onto a fixed cell array —
    so the plain, 8-colour, 256-colour and truecolour renderings occupy the same columns by
    construction rather than by four code paths agreeing.

    `eta` is the CALLER's string. This function does no timing: a duration derived from a single
    sample is exactly the invented progress this module's header forbids.
    """
    try:
        g = _glyphs(stream)
        w = max(1, int(width))
        # 1. geometry, in visible cells
        try:
            f = float(frac)
            if f != f:                      # NaN
                raise ValueError
        except (TypeError, ValueError):
            f = None
        if f is None:
            filled, empty = 0, w
        else:
            f = min(1.0, max(0.0, f))
            filled = int(f * w)             # floor: round() shows a full bar at 97.9%
            if filled == 0 and f > 0.0:
                filled = 1                  # any real progress must be visible
            if filled == w and f < 1.0:
                filled = w - 1              # only 100% looks finished
            empty = w - filled
        assert filled + empty == w

        # 2. colour the fixed cells
        d = color_depth(stream) if depth is None else depth
        full, void = g["full"], g["void"]
        if not d:
            body = full * filled + void * empty
        elif not rich(d):
            # 8-colour has no orange; a red+yellow "ramp" reads as a barber pole, i.e. a fault.
            body = f"{_MAG}{full * filled}{_OFF}" + _dim(void * empty, d)
        else:
            a, b = brand(ramp[0]), brand(ramp[1])
            body, prev = "", None
            for i in range(filled):
                # ramp across the FULL width: if it were rescaled to `filled`, every cell's hue
                # would shift on each tick and the bar would read as a rendering glitch.
                t = i / max(1, w - 1)
                rgbv = tuple(int(a[j] + (b[j] - a[j]) * t) for j in range(3))
                code = rgbv if d == 24 else rgb256(*rgbv)
                if code != prev:            # run-length: ~6 escapes instead of one per cell
                    body += sgr(rgbv, depth=d, stream=stream)
                    prev = code
                body += full
            body += _OFF + _dim(void * empty, d)

        # 3. trailing text, padded by visible width
        pct = "  —  " if f is None else vpad(f"{f * 100:.0f}%", 4, align="right")
        tail = f"  {pct}"
        if label:
            tail += f"  {label}"
        if eta:
            tail += f"  {eta}"
        return body + (_dim(tail, d) if d else tail)
    except Exception:
        return ("#" * max(0, int(width or 0)))[:int(width or 0)]

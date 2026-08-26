#!/usr/bin/env python3
"""Preview the animated wait banner (the Ascend logo shown during `assess run`).
Three tiers, auto-selected:
  image    the real logo PNG inline (iTerm2 / WezTerm / Kitty / Ghostty)
  logo     the logo shape in breathing red half-blocks, glint sweeping the blade (VS Code, Apple
           Terminal, any truecolor / 256-color terminal)
  wordmark the twinkling ASCEND wordmark (mono / no-color)
Force a tier with ASCEND_LOGO=image|block|wordmark|off.

    python3 demo/preview_wait.py
"""
import sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shells" / "cli"))
import ascend  # noqa: E402  (sets up its own import paths on load)

tw = ascend._TwinkleBanner("running · local · starting")
if not tw.enabled:
    print("Not a TTY (or --json): the banner is intentionally silent here. Run it in a terminal.")
    sys.exit(0)
labels = {
    "image": f"real logo image ({tw._proto})",
    "logo": "braille logo (" + ("24-bit" if tw._tc else "256-color") + ")",
    "wordmark": "ASCEND wordmark (no color / no unicode)",
}
# Simulate a run: 240 probes completing over ~18s, with a handful of failures, so the feed streams.
TOTAL = 240
plan = [(0.04, 0), (0.14, 1), (0.28, 3), (0.44, 5), (0.61, 8), (0.78, 11), (0.92, 14), (1.0, 17)]
with tw:
    for prog, failed in plan:
        tw.push_progress(int(round(prog * TOTAL)), failed, TOTAL)
        tw.set_subtitle(f"running · local · in progress  {int(round(prog * 100))}%")
        time.sleep(2.3)
print(f"\nrendered: {labels.get(tw._mode, tw._mode)}")

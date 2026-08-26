#!/usr/bin/env python3
"""Probe whether this terminal can render the ACTUAL Ascend logo (pixel-perfect, not ASCII) via the
iTerm2 inline-image protocol, and report which terminal you are in.

    python3 demo/preview_logo_image.py

If you see a small red Ascend star below the "banner size" and "chat-bullet size" labels, your
terminal supports real inline images and I can use the real logo everywhere. If you see a blob of
base64 text or nothing, it does not (Apple Terminal.app is the common case) and we use the fallback.
"""
import base64
import os
import sys
from pathlib import Path

PNG = Path(__file__).resolve().parents[1] / "assets" / "ascend-logo.png"
data = PNG.read_bytes()
b64 = base64.b64encode(data).decode()


def iterm_seq(width_cells: int) -> str:
    payload = (f"1337;File=inline=1;width={width_cells};preserveAspectRatio=1;"
               f"size={len(data)}:{b64}")
    seq = f"\033]{payload}\a"
    if os.environ.get("TMUX") or os.environ.get("TERM", "").startswith(("screen", "tmux")):
        seq = "\033Ptmux;" + seq.replace("\033", "\033\033") + "\033\\"   # tmux passthrough
    return seq


print("terminal identity")
for k in ("TERM_PROGRAM", "TERM_PROGRAM_VERSION", "LC_TERMINAL", "COLORTERM", "TERM",
          "KITTY_WINDOW_ID", "WEZTERM_PANE", "TMUX"):
    print(f"  {k:<20} = {os.environ.get(k) or '(unset)'}")

if not sys.stdout.isatty():
    print("\nstdout is not a TTY, so no image is drawn. Run this directly in your terminal.")
    sys.exit(0)

print("\nbanner size (what would show while waiting):")
sys.stdout.write(iterm_seq(12))
sys.stdout.write("\n\nchat-bullet size (what would prefix a chat reply):\n")
sys.stdout.write(iterm_seq(2))
sys.stdout.write("  the bot reply would start here...\n\n")
print("If those two spots show a red star, real inline images work here. Tell me what you saw.")

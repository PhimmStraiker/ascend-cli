#!/usr/bin/env python3
"""
preview_ui.py — render every presentation primitive at every colour depth.

No test can see colour: the suite always runs piped, so `color_ok()` is False and the styled
branch never executes. This is how that branch gets looked at.

    python3 demo/preview_ui.py            # all four depths
    python3 demo/preview_ui.py --depth 8  # just one
    python3 demo/preview_ui.py --animate  # watch the bar advance

Read-only. Prints and exits.
"""
import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))
import ui  # noqa: E402

DEPTHS = (0, 8, 256, 24)


def block(depth):
    os.environ["ASCEND_COLOR_DEPTH"] = str(depth) if depth else "1"
    os.environ.pop("NO_COLOR", None)
    os.environ["ASCEND_FORCE_COLOR"] = "1"
    name = {0: "plain (piped / NO_COLOR / ASCEND_PLAIN)", 8: "8-colour",
            256: "256-colour", 24: "truecolour"}[depth]

    print()
    print(ui.rule(72))
    print(f"  depth {depth} — {name}")
    print(ui.rule(72))
    print()
    print(ui.header("ascend", subtitle="assess run", stream=sys.stdout))
    print()
    print(ui.section("progress", stream=sys.stdout))
    for f in (0.0, 0.07, 0.42, 0.85, 1.0, None):
        print("    " + ui.gradient_bar(f, width=28, depth=depth, stream=sys.stdout,
                                       eta="5s" if f not in (0.0, None) else ""))
    print()
    print(ui.section("states", stream=sys.stdout))
    row = "    "
    for w in ("serving", "dead", "paused", "GONE", "complete", "some_new_status"):
        row += ui.state(w, width=16, stream=sys.stdout)
    print(row)
    print()
    print(ui.section("key / value", stream=sys.stdout))
    for ln in ui.kv([("tenant", "acme (1 app)"), ("adapter", "sse_stream"),
                     ("endpoint", "https://your-bot.example.com/api/chat"),
                     ("key", "tc-9547…d0")], stream=sys.stdout, indent="    "):
        print(ln)
    print()
    print(ui.panel(
        ["1 bridge-based app has a live assessment and nothing is answering it. "
         "Unanswered probes are not findings, so the run will finish looking CLEAN "
         "while measuring nothing (a FALSE PASS)."],
        title="NO BRIDGE", tone="alarm", hint="start it:  ascend bridge start --app 'My Bot'",
        stream=sys.stdout, width=76, indent="    "))
    print()
    print(ui.panel(["adapter proven against the live target"], title="VALIDATED", tone="ok",
                   stream=sys.stdout, width=76, indent="    "))
    print()
    print(ui.section("pass / fail bar (existing helper)", stream=sys.stdout))
    print(f"    {ui.bar(41, 7, cell=12)} 41 passed / 7 failed")


def animate():
    os.environ["ASCEND_FORCE_COLOR"] = "1"
    os.environ.pop("NO_COLOR", None)
    os.environ.pop("ASCEND_COLOR_DEPTH", None)
    t0 = time.time()
    print("\n  assess watch, as a terminal sees it (ctrl-c to stop)\n")
    try:
        for i in range(0, 101):
            el = time.time() - t0
            sys.stdout.write("\r\033[K  " + ui.gradient_bar(
                i / 100, width=28, stream=sys.stdout,
                label="status=running  failed=3/48",
                eta=(f"{int(el)}s" if el >= 3 else "")))
            sys.stdout.flush()
            time.sleep(0.05)
        print()
    except KeyboardInterrupt:
        print()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--depth", type=int, choices=list(DEPTHS))
    ap.add_argument("--animate", action="store_true")
    a = ap.parse_args()
    if a.animate:
        return animate()
    for d in ([a.depth] if a.depth is not None else DEPTHS):
        block(d)
    print()


if __name__ == "__main__":
    main()

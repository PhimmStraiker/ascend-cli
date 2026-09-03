# Ascend CLI 1.1 — one noun, one command, from a URL to a running assessment.
#
#   export STRAIKER_PAT=s6r_pat_...        # vhs inherits the environment
#   vhs demo/target-flow.tape              # -> demo/target-flow.gif + .mp4
#
# This replaces the pre-1.1 arc (`map` -> `adapter build` -> `app create` -> `relay ls`), which
# is still supported but is no longer how anyone should be taught the tool. Everything here goes
# through `target`.
#
# Deterministic and safe to re-record: the target is the LOCAL echo agent on :8600, so a
# recording never depends on a third party being up. It does talk to a real tenant, creates one
# application called "Demo Bot", and removes it again at the end.
#
# Nothing secret reaches the screen: `target add` masks the bridge key it stores, and no command
# here prints a PAT. Do not add `keys list`.
#
# NOTE on syntax: `Type` lines contain no shell ${VAR} expansions. vhs 0.11 rejects `$` inside a
# Type argument at validate time, which is why the older tapes in this directory no longer pass
# `vhs validate`. Environment comes from the shell that launches vhs instead.

Output demo/target-flow.gif
Output demo/target-flow.mp4

Set Shell "bash"
Set FontSize 14
Set Width 1760
Set Height 900
Set Theme "Catppuccin Mocha"
Set TypingSpeed 40ms
Set Padding 22

# --- hidden setup ------------------------------------------------------------------------
Hide
Type "export COLORTERM=truecolor" Enter
Type "python3 demo/localhost_agent.py --port 8600 >/tmp/agent.log 2>&1 & sleep 2" Enter
Type "clear" Enter
Show

Type "# Ascend CLI 1.1 — a target is one thing now, not four."               Enter
Sleep 1500ms

Type "# 1. Can we reach the platform at all?"                                Enter
Sleep 500ms
Type "./ascend doctor"                                                       Enter
Sleep 6s

Type "clear"                                                                 Enter
Type "# 2. Add the target. Give it a URL, a request copied out of devtools," Enter
Type "#    or a .har — it works out which, derives the adapter, PROVES it"   Enter
Type "#    against the live endpoint, registers the app, stores the key."    Enter
Sleep 800ms
Type "./ascend target add http://127.0.0.1:8600/chat --save-as demo-bot --name 'Demo Bot'" Enter
Sleep 14s

Type "clear"                                                                 Enter
Type "# 3. What do I have? Adapter, config, registration, and whether"       Enter
Type "#    anything is serving it — one line each."                          Enter
Sleep 800ms
Type "./ascend target list"                                                  Enter
Sleep 5s

Type "# Everything bound to one target, in one place:"                       Enter
Sleep 500ms
Type "./ascend target show 'Demo Bot'"                                       Enter
Sleep 6s

Type "clear"                                                                 Enter
Type "# 4. Re-prove it before spending an assessment. A target that has"     Enter
Type "#    drifted still produces a clean-LOOKING run that measured"         Enter
Type "#    nothing — this is what separates a pass from a false pass."       Enter
Sleep 800ms
Type "./ascend target check 'Demo Bot'"                                      Enter
Sleep 6s

Type "clear"                                                                 Enter
Type "# 5. Run it. The bridge is started and stopped for you — there is no"  Enter
Type "#    separate process to go and find."                                 Enter
Sleep 800ms
Type "./ascend assess run --app 'Demo Bot' --name 'demo run' --no-wait"      Enter
Sleep 12s

Type "clear"                                                                 Enter
Type "# 6. Watch it. Progress is measured, never invented."                  Enter
Sleep 600ms
Type "./ascend assess watch --app 'Demo Bot'"                                Enter
Sleep 25s
Ctrl+C
Sleep 1s

Type "clear"                                                                 Enter
Type "# Every command also takes --json, for an agent or a pipeline."        Enter
Sleep 600ms
Type "./ascend target list --json"                                           Enter
Sleep 6s

# --- hidden teardown: leave the tenant as we found it -------------------------------------
# Deleting the application also stops its bridge and drops its stored key.
Hide
Type "./ascend bridge stop --all >/dev/null 2>&1" Enter
Type "./ascend target rm 'Demo Bot' >/dev/null 2>&1" Enter
Type "pkill -f localhost_agent.py" Enter
Show

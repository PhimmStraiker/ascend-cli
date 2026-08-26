---
name: onboard-target
description: >-
  End-to-end onboarding of a new red-team target: discover its shape, build and
  validate an adapter config, register a thin Ascend app, prove one live probe
  round-trips through the bridge, then launch the first assessment. Use when you
  have a fresh target (an API, chat widget, agent, or bot) and nothing set up yet.
---

# onboard-target

The full path from "here is a target" to "an assessment is running". It composes
the other skills and the deterministic CLI in order, with a **live probe gate** in
the middle so you never launch an assessment against a config that cannot actually
reach the target.

`ascend` below means `python3 shells/cli/ascend.py`.

## Preconditions
- `$STRAIKER_PAT` is set (tenant PAT). Confirm reachability first:
  ```
  ascend --json doctor
  ```
  PAT exchange, control catalog, and bridge reachability must be green before you
  proceed.
- You have captured evidence of one answered turn from the target (HAR, in-page
  capture, or a proxied send). If not, run **recon** first.
- Rules of engagement agreed: target allowlist, QPM cap, side-effect budget.

## Workflow

### 1. Discover + build a validated adapter
Follow the **build-adapter** skill. Do not continue past it until
`ascend adapter validate --config <config>` is **green**. A thin app and an assessment
built on an unvalidated config waste the whole run.

Output of this step: a config name (e.g. `mybot`) and its adapter type (a
transport or preset from `ascend adapter list`).

### 2. Pick the starting controls
Keep the first run tight — validate transport before spending a big control
budget. List and validate a small, relevant set (see **recon** for mapping
surface → controls):
```
ascend --json controls list --category <cat>
ascend --json controls validate <id1,id2,...>
```
Heed warnings: a selection that generates **zero probes** is a no-op. Drop
deprecated ids unless you have a reason.

### 3. Register a thin app (get the tc key)
```
ascend app create --type bridge --name "<display name>" \
  --system-prompt "<optional description>" \
  --controls <validated,ids> --size small --qpm <roe_cap>
```
This prints the `tc_key` (`thin_api_key`) **once**. Capture it into the
environment immediately — it is not retrievable later:
```
export STRAIKER_BRIDGE_API_KEY=tc-...
```
Note the returned `app_id` (`aapp_...`).

> No customer names anywhere — the display name is a neutral target label.

### 4. Live probe gate — prove one round-trip
Start the pull-mode bridge against the validated config at a **trivial rate** and
confirm exactly one probe relays to the target and returns its real answer:
```
STRAIKER_BRIDGE_API_KEY=tc-... \
  ascend runtime start --adapter <type> --config <config> \
  --qpm 2 --max-workers 1 --capture ./captures/onboard_probe.jsonl
```
Watch the log for a leased probe → target call → result. Stop it (Ctrl-C) after
one clean round-trip. Inspect the capture (it is redacted + 0600):
```
tail -n 2 ./captures/onboard_probe.jsonl
```
The relayed result must be the target's genuine answer — not an auth error, not an
empty body, not a transport parse failure. If it is wrong, go back to
**build-adapter** step 5 (iterate the failing layer) — **do not** launch the
assessment. This is the same gate build-adapter uses; here it doubles as an
end-to-end bridge check (lease → adapter → target → result).

### 5. Launch the first assessment
With the probe gate green, run it for real:
```
ascend --json assess run --app <app_id> --name "onboarding run 1" \
  --controls <validated,ids>
```
`assess run` does the correct lifecycle (create → pause → resume → poll) and
blocks until terminal. Keep the **runtime** from step 4 running in a separate shell
so it can service the leased probes (re-start it without `--qpm 2` / capture, at
the ROE cap). For a long run, pass `--no-wait` and monitor with the
**run-assessment** skill.

### 6. Hand off
- Assessment launched and progressing (`ascend assess status`).
- When it completes, go to **run-assessment** (monitor/results) then
  **triage-findings** (FP triage + severity recalc).

## Definition of done
- `doctor` green; adapter config **validated**; one **live probe** round-tripped
  the real target answer through the bridge; thin app registered; first assessment
  running against a non-zero-probe control selection.

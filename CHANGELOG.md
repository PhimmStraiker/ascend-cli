# Changelog

All notable changes to the Ascend CLI. Newest first. Format follows
[Keep a Changelog](https://keepachangelog.com/); versions follow [SemVer](https://semver.org/).

---

## [Unreleased]

### Added
- **`adapter validate` now says when a target cannot be assessed at all.** A config can be perfectly
  correct and still unusable: the platform bounds how long each probe may take (~120s), and that
  clock starts when the probe is **queued**, not when the bridge calls the target. Exceeding it
  surfaces as a synthetic failure indistinguishable from a target error, which feeds the platform's
  target-health streak and auto-pauses the assessment — so the run reports no findings having
  measured nothing. `validate` now reports the measured reply time and warns when it is at or beyond
  the window ("raising the adapter timeout does NOT help") or close enough that queueing alone can
  blow it. The operator learns this from one probe instead of from a whole failed run.
- **`$ASCEND_PLATFORM_PROBE_WINDOW_MS`** sets the window the CLI assumes the platform enforces. It
  exists so that when the platform-side window is raised for agentic targets, nothing in the CLI
  needs a code change — set the env var and the warnings, and the guidance they carry, move with it.

## [1.1.0] — 2026-09-01

### Known limitation — slow targets are bounded by the platform, not the CLI
The platform gives a bridge a bounded window to return a probe result (`probe_shadow`'s
`BRIDGE_RESPONSE_TIMEOUT`, on the order of 100-120s), and the bridge gives up just under it rather
than hold a worker open for a result nobody will accept. **A target that reliably takes longer than
~110s cannot be assessed through the bridge today**, whatever the adapter's `timeout_ms` says —
agentic targets that take 2-3 minutes are already past it. Raising the adapter timeout alone does
not help and only wastes a target call. The server-side window has to be raised first; the bridge
side is now configurable to match, via `bridge_response_timeout_ms` in the config or
`$ASCEND_BRIDGE_RESPONSE_TIMEOUT_MS` (default 110s, previously a hardcoded 110s).

### Fixed
- **The target timeout no longer assumes a fast target.** Adapters each hardcoded their own 30-60s
  ceiling and `adapter build` pinned the *discovery* timeout (often 20-30s) into the config it
  wrote. Agentic targets routinely reply in 2-3 minutes and some take far longer, so those values
  turned a healthy slow target into 100% probe failures — and because failed probes make the
  platform auto-pause the assessment, it presented as "the bridge broke". There is now one resolver
  (`adapters.base.resolve_timeout_s`): the config's `timeout_ms` wins, else
  `$ASCEND_TARGET_TIMEOUT_MS`, else a default that sits **above** the ~10 minute envelope we intend
  to serve, always clamped to `$ASCEND_TARGET_MAX_TIMEOUT_MS` so a genuinely hung target still
  cannot hold a worker open for the whole run. `adapter build` no longer pins a short value.
  The default was set by measurement, not taste: a live 10-minute target failed under a 5 minute
  default at exactly 300s, then failed *again* under a 10 minute default because a timeout equal to
  the reply time has no headroom and loses the race by a second or two. A timeout is an upper bound,
  so it has to sit above the slowest reply it is meant to serve. (Through the bridge the platform's
  own response window is the tighter limit — see the known limitation above.)
- **Dead relay state is pruned.** Every app ever served left pid/status files behind, so `bridge ls`
  filled with corpses (one was still listed 173 hours after it died) and a relay that was genuinely
  wrong got lost in the noise. Relays dead for more than a day are dropped from the listing;
  recently-dead ones are kept for triage, and logs are never removed.
- **A bridge now stops only for the assessment it is bound to.** The relay used to infer "my work is
  done" from *every* assessment on the app (or simply the newest one). A finished unrelated run, or
  a gap before the next run existed, therefore read as "all done" and reaped a relay that was still
  serving live probes. The stop decision is now scoped to the bound run, and a relay with **no**
  bound run never self-stops on a terminal status — which is also what makes a standalone
  `ascend runtime start` genuinely persistent. `assess run` binds the real assessment id to the relay
  as soon as the platform names it (a relay must be up *before* the run is created, so it cannot get
  the id from argv).
- **A hand-started relay is now visible to the rest of the CLI.** `ascend runtime start` wrote no
  pidfile at all (only the supervised path did) and only wrote status when `--status-file` was
  passed, so `bridge ls` and `is_serving()` could not see it. `assess run` therefore concluded no
  bridge was serving and started a **second** relay for the same app, with two consumers splitting
  that app's probes — the "probes stopped flowing" report. A relay now registers itself under the
  app it serves and deregisters on exit, so a standalone bridge is reused rather than duplicated.
  This is what makes standalone and CLI-managed bridges mutually exclusive in practice.
- **A manually started relay registered itself under the config name instead of the app id.**
  `runtime start --config acme` filed its state as `acme`, so `is_serving(aapp_…)` could never see
  it, the auto-lifecycle concluded no bridge was running, and it started a **second** relay for the
  same app — two consumers splitting one app's probes, which presents as "probes stopped flowing".
- **`assess run` no longer kills a relay it does not own.** It releases only a bridge it started
  itself; a reused or standalone relay is left running.
- **The heartbeat is written before the reconcile network call.** Liveness is judged by heartbeat
  age, so a slow control-plane call could push a perfectly healthy bridge toward "stale".
- **`app create` no longer sends a payload the platform always rejects.** With no `--controls` the
  CLI sent `control_type: "all"`, which v3 rejects (400 "rejected by the upstream service") — and
  omitting the field is rejected too. The only accepted shape is `custom` plus an explicit id list,
  so the CLI now resolves the control catalog and registers with every non-deprecated control.
- **`app create` recovers from a lost response.** The POST is routinely dropped *after* the platform
  created the app ("Response ended prematurely"), which reported a failure for an app that exists and
  led operators to retry into duplicates. It now re-reads the app by name and reports it as
  recovered. The bridge key survives this: `thin_api_key` is **not** write-once — the platform
  returns it on GET and in the app list — so when a create response arrives without it the CLI reads
  it back off the app instead of leaving an app no bridge can serve.

### Added
- **The `auth_lifecycle` block is now actually wired in, for every adapter.** A short-lived
  credential — a mobile app's bearer, an OAuth access token — expires part-way through a long run;
  every later probe then returns 401 and scores as a target "refusal", so the assessment finishes
  looking clean while measuring nothing. The decision layer for this already existed
  (`layers/auth.py` `AuthLifecycle`, with `static | refresh_on_ttl | reauth_on_401 |
  cookie_rotation`, JWT `exp` awareness and a configurable challenge status) and discovery already
  wrote the block onto every composed config — but nothing read it at runtime. It is now applied at
  the shared call seam (`call_target`), so a challenge response re-acquires credentials and retries
  the probe once, for **every** adapter rather than a chosen few. An `oauth2` config with no
  explicit block keeps its previous fixed-TTL refresh. Verified live against a token-gated target:
  4/4 probes across two token expiries.
- **`demo/localhost_agent.py` QA fixtures.** `--slow-secs` simulates a slow/agentic target (agents
  commonly take 2-3 minutes) and `--token-ttl` requires a short-lived bearer from `POST /token`, so
  both the bridge's behavior against long-running targets and the adapter auth lifecycle can be
  proven against a real server instead of a mock.
- **`qa/live_lifecycle.sh`** — the live ship gate: real platform, real app, real agent, asserting the
  invariants that unit tests kept missing (relay registered under the app id, unbound relay stays
  up, probes actually answered, relay released after the run).

### Changed
- **App type `thin` is now `bridge`** everywhere a user sees it: `app create --type` choices are now
  `bridge|api|gcp|bedrock` (default `bridge`), and `app create|list|status` output and help all say
  `bridge`. The v3 API wire value is unchanged (`api_type: "thin"` internally). Only the
  user-facing label moved, so there is no API or protocol change.
- **The bridge is auto-managed.** `ascend assess run` on a bridge-type app auto-starts the CLI's
  built-in relay *before* probes are scheduled, and the bridge self-stops when the assessment reaches
  a terminal state. While an assessment is paused the bridge stays alive and keeps serving (idle
  cleanup is opt-in via `--idle-timeout`, off by default). `ascend assess resume` re-ensures a bridge
  after a Console-side resume, since the SaaS cannot start a process on your machine. `ascend bridge start` still exists for
  advanced/remote/continuous/pre-start use but is no longer a required step in the normal flow.
- A bridge is **per-app**: one relay is shared across that app's assessments with no cross-assessment
  contamination. The v2 lease/result protocol carries only opaque `request_id`/`msg_id` that the
  bridge echoes back, and the platform attributes each probe to its assessment.

### Added
- **`ascend assess run` supervises its bridge.** While a run is followed (the default), every poll
  re-ensures the relay: if the bridge dies mid-run for any reason, it is restarted so probes keep
  flowing (a dead bridge scores a FALSE PASS). A live bridge is a no-op, native apps are skipped, and
  the watchdog never raises. No external watchdog script needed.
- **`ascend doctor` reports current vs latest version, and can update in place.** doctor now shows
  the packaged version against the latest **published GitHub release** and, when behind, prints the
  exact upgrade command for how this copy was installed. "Latest" tracks published *releases* only,
  so tags and pushes to `main` stay invisible; an update surfaces only when
  a release is cut. A release body may carry a `min-supported: X.Y.Z` marker (or a `[security]`
  token): if the running version is below it, doctor flags the update as **recommended**. It is a
  soft check and never changes doctor's exit code. `ascend doctor --update` performs the update for
  a git-clone install (`git pull --ff-only`) and prints the command for pipx/binary installs. The
  check is a single unauthenticated GitHub request, no PAT and no telemetry, disabled by
  `ASCEND_NO_UPDATE_CHECK`.
- **`$ASCEND_BRIDGE_IDLE_TIMEOUT` enables idle cleanup for auto-managed runs.** Idle cleanup is off
  by default; the bridge stops when its run reaches a terminal state. Set this variable to a positive
  number of seconds to enable idle cleanup on every path, including the `assess run` and
  `assess resume` flows that start the bridge for you and cannot take a `--idle-timeout` flag.
  Contributed by a design partner.
- `ascend bridge sync` — reconciles local bridges to platform assessment state (start for
  running/paused apps, stop for terminal). The manual fallback when state changed in the Console.
- **Live run view for `ascend assess run`.** While an assessment runs, the terminal shows the Ascend
  logo header (the weapon-star drawn in braille next to the `ASCEND` wordmark) and a live probe feed:
  each completed probe streams in as a red-star-bulleted line with `pass`/`FAIL`, paced to read like
  the Console live view. Driven by the run's aggregate progress counts (no live prompt text). Three
  render tiers, auto-selected: the real logo PNG inline on image-capable terminals
  (iTerm2/WezTerm/Kitty/Ghostty), the braille logo on any truecolor or 256-color terminal (VS Code,
  Apple Terminal), and the `ASCEND` wordmark as a mono fallback. TTY-only: scripts, pipes, agents, and
  `--json` render nothing. Override the tier with `ASCEND_LOGO=image|block|wordmark|off`.

### Removed
- `ascend app create-thin` — use `ascend app create --type bridge` (`bridge` is the default type, so
  `ascend app create` already creates a bridge app).

### Safety
- False-pass safety is preserved: a bridge never self-stops when it cannot verify assessment state.

### Fixed
- **The bridge no longer dies between recon rounds.** The self-reconcile decision classified any
  assessment status outside a hand-written running list as terminal, so an intermediate recon-phase
  status the platform emits between rounds read as "done" and the bridge stopped itself after every
  round. It now treats a run as over only when every assessment is EXPLICITLY terminal (the same
  `TERMINAL_STATUSES` set the rest of the CLI uses); any unrecognized or intermediate status keeps
  the bridge serving, honoring the never-self-kill-when-unsure rule. A termination grace also rides
  through a brief all-terminal gap between rounds before stopping.
- **The CLI imports on Python 3.9–3.11 again.** A nested same-quote f-string in `ascend discover`
  is a SyntaxError before Python 3.12, so `ascend` failed to start on the 3.9–3.11 range listed in
  `pyproject.toml`. It is now a plain string join. Reported and fixed by a design partner.
- **The bridge no longer self-stops while a run is stalled.** Previously the relay idle-timeout (30
  min) treated a `created`/pending run the same as a paused one, so a platform stall would
  reap the bridge and strand the run. The bridge now stops only when the run
  reaches a terminal state and rides through `created`/`paused`/stalled states. Idle cleanup is now
  opt-in via `--idle-timeout` (0 by default), and reaps only a paused run that actually relayed a
  probe and then went quiet.
- **Results are delivered with retry, and delivery is counted separately from answering.** A failed
  `submit_result` used to be logged and dropped, so a computed result cost a target call and a ~90s
  server reclaim, then the probe was re-issued and re-run (a cause of runs crawling).
  Submissions now retry with backoff, and the relay tracks `delivered`
  (server-acked) separately from `answered` (handler produced a result). When `delivered` lags
  `answered`, results are being dropped, which was previously invisible because only `answered` was reported.
- **`/v2/result` no longer inherits the lease long-poll timeout.** The result POST shared the
  `(wait_ms + 10)s` ceiling of the `/v2/lease` long-poll; it now uses a separate, shorter
  `result_timeout` (10s), so a slow ack fails fast and retries. The retry budget is bounded (a few
  attempts within the reclaim window) so a delivery storm cannot stall the lease loop.
- **`ascend bridge ls` surfaces lease and submit errors.** The table adds `DELIV`, `LEASE-ERR`, and
  `SUB-ERR` columns and warns when timeouts are present, so a relay in a lease-service timeout storm
  no longer reads as healthy. `ascend bridge start` gains `--idle-timeout` so idle cleanup can be
  opted into without dropping to `runtime start`.
- **The lease long-poll no longer times out on normal server-side hold jitter.** `/v2/lease` is a
  long-poll: the platform holds the connection open up to `wait_ms` (25s) waiting for a probe, but
  the client read-timeout allowed only 10s of headroom over that hold, so an ordinary long hold
  during a probe drought was recorded as a "lease error." The margin is now 25s (`lease_margin`,
  total ceiling 50s), so a lease read-timeout again signals a genuine problem.
- **Slow agentic targets are no longer cut off at ~30s.** Every HTTP/streaming adapter defaulted its
  per-target timeout to 26–30s to "stay under the 30s bridge ceiling." That was fine for a fast chatbot but
  severed any agent that legitimately takes longer to answer. That ceiling was stale; the cap
  is the platform's ~90s probe-reclaim window. The default is now 60s (still overridable per target
  via `timeout_ms`), which leaves headroom for the result to be delivered within that window
  (handler time plus result delivery must fit under ~90s, so 60s handler + delivery stays clear). The
  bridge also now leases at most `max_workers` probes at a time, so a serial (stateful) target no
  longer holds a batch of probes it cannot acknowledge before the server reclaims them. Targets that
  answer in more than ~90s additionally require the platform to extend the reclaim window (or add
  lease renewal), a coordinated change the client cannot make on its own.

---

## [1.0.0] — initial release

First release for the SE team. A single, scriptable CLI that connects the **Straiker Ascend**
assessment cloud to any AI target and runs a red-team assessment end to end.

The model is **Iris → Bridge → Adapter → App**: the bridge is generic; the *adapter* is the
per-app piece that knows how to talk to one specific target.

### Connecting to targets
- `ascend adapter build` derives a **validated** adapter from a HAR, a cURL, an OpenAPI spec, a live
  URL (drives a real browser), or an API endpoint, and proves it against the live target before
  writing anything. An unvalidated config is never saved.
- 15 built-in adapters (REST/JSON, SSE and marker/sentinel streaming, WebSocket, multi-step session
  APIs, browser widgets, and the platforms: Salesforce Agentforce, Slack, Vertex AI, Copilot
  Studio, Amazon Connect, AWS Bedrock).
- **Per-app adapters as code**: when no built-in pattern fits, `--code` generates a self-contained
  adapter module for that one app and proves the generated code live.
- **Anti-automation targets** (endpoints that 403 any non-browser replay) are handled
  automatically: `adapter build --url` falls back to a generated **browser** adapter, driven and
  validated through a real browser.
- Auth-first throughout: bearer, API key, basic, cookie, login/access-code flows, mTLS, custom CA,
  proxy; an SSRF guard that allows internal RFC-1918 targets but blocks cloud metadata.

### Running and managing assessments
- `app create` (types `thin | api | gcp | bedrock`), `bridge start` (the CLI *is* the bridge; one
  per app, keyed and adapter-bound), `assess run/watch/pause/resume`, and a single-tenant lock so
  an SE cannot cross customers.
- Local `tc-` bridge-key store, one per app; keys are shown once and never printed in full.

### Reading results
- `ascend results` — assessments as a table, or a Console CSV export analysed in depth: rollups by
  the platform's own taxonomy (risk tag, category, control, data class) and by evasion technique,
  a data-harvest view with value provenance, and a guardrail confusion matrix.
- `ascend ci` — pipeline gate with a stable exit-code contract (`0` clean · `1` could not
  read/trust the results · `2` findings). Fails safe: a run that measured nothing (dead bridge,
  server-side failure, undeterminable severity) is never reported as a pass.
- `ascend export` — SARIF / Markdown / CSV / JSON.

### Agent- and CI-friendly
- One-object-per-call JSON on `--json` (success and failure), human prose to stderr only, so
  redirecting one never corrupts the other. Idempotent create flags (`--if-not-exists`).

### Safety properties
- Nothing is written that did not answer the live target. Unanswered probes are never counted as
  passes. `doctor --api-compat` watches for API drift. One tenant per machine.

# Changelog

All notable changes to the Ascend CLI. Newest first. Format follows
[Keep a Changelog](https://keepachangelog.com/); versions follow [SemVer](https://semver.org/).

---

## [Unreleased]

### Added
- **`ascend target` — one noun for the thing you actually assess.** A target used to be spread
  across an adapter config, an application record, a stored key and a purpose string, and you had
  to hold all four in your head and keep them in sync. `target add | list | show | check | rm` is
  now the everyday surface: `show` puts everything bound to a target in one place, `check`
  re-proves it against its live endpoint and times it, and `list` says which are registered and
  which are serving. `app`, `adapter` and `keys` are unchanged underneath and still fully
  supported — nothing was removed or renamed.
- **`target add <thing>` works out what you gave it.** A URL, a request copied out of devtools, an
  exported browser session, or a saved config — it detects which and onboards from it. Choosing
  between five mutually-exclusive source flags was a question people often could not answer; the
  artifact itself says which it is. It stops once the target is registered and proven, because
  spending an assessment is a separate decision (`--run` to continue straight into one).

### Changed
- The skills carry a troubleshooting playbook and a per-target-pattern catalog: which adapter
  suits which target shape, and the way each one specifically fails. Most failures here present
  as a different failure than they are, so the playbook is ordered by symptom and starts with the
  one number that settles it — `ANS` in `bridge ls`.
- `ascend --help` leads with the one command that does the whole flow (`onboard`) and shows the
  seven you use day to day, with the rest listed by name. 71 lines to 44. No command changed or
  moved — every one still runs exactly as before.

### Added
- `adapter validate` reports the target's measured reply time and warns when it cannot survive the
  platform's per-probe window — a config can be correct and the target still unassessable. Learned
  from one probe instead of from a whole failed run.
- `$ASCEND_PLATFORM_PROBE_WINDOW_MS` sets the per-probe window the CLI assumes the platform
  enforces. It is the only timeout knob: the bridge's give-up point and the adapter's own timeout
  are derived from it, so raising the platform-side window is a config change, not a release.

## [1.1.0] — 2026-09-01

Relay management was the consistent failure point for customers ("the bridge keeps dying", "probes
stopped flowing"). Every fix below was found by running the CLI against the real platform.

### Fixed
- **Relay lifecycle.** A relay now stops only for the assessment it is bound to (it used to infer
  "done" from any assessment on the app, and reap itself mid-run); an unbound relay never
  self-stops, which is what makes a standalone `runtime start` persistent. A hand-started relay
  registers itself under the app it serves, so the CLI can no longer start a second relay for the
  same app and split its probes. `assess run` releases only a relay it started, and only once the
  run is genuinely terminal. Dead relay state older than a day is pruned from `bridge ls`.
- **Credentials that expire mid-run.** The `auth_lifecycle` block (`static | refresh_on_ttl |
  reauth_on_401 | cookie_rotation`) is applied at the shared call seam, so an auth challenge
  re-acquires credentials and retries the probe once for every adapter. Previously an expired token
  turned every later probe into a 401 that scored as a target refusal.
- **`app create --type bridge` without `--controls`.** The CLI sent a control selection the platform
  rejects; it now resolves the control catalog itself. `create_app` also recovers when the response
  is dropped after the app was created, instead of reporting a failure that succeeded.
- **Target timeouts.** Adapters each hardcoded a short ceiling and `adapter build` pinned the
  discovery timeout into the config it wrote, which turned a healthy slow target into 100% probe
  failures. One derived value now governs, and `adapter build` no longer pins one.

### Known limitation
The platform bounds how long each probe may take (~120s), and the clock starts when the probe is
**queued**, not when the bridge calls the target. A target that reliably takes longer cannot be
assessed through the bridge, whatever `timeout_ms` says — agentic targets at 2-3 minutes per turn
are past it. Raising the adapter timeout does not help; the platform-side window has to be raised
first, and then `$ASCEND_PLATFORM_PROBE_WINDOW_MS` tells the CLI about it.

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

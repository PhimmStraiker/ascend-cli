# Changelog

All notable changes to the Ascend CLI. Newest first. Format follows
[Keep a Changelog](https://keepachangelog.com/); versions follow [SemVer](https://semver.org/).

---

## [Unreleased]

### Security
- **`app list` and `app get` no longer print bridge keys.** The platform returns `thin_api_key` on
  GET and in the application list, not only at creation, so a read-only listing emitted every
  bridge-type app's key in full — into CI logs, agent transcripts and screen shares, from the
  command least likely to be suspected of holding a secret. They are masked now, as `creds` already
  promised everywhere else. `app create` still shows the key once, on purpose.
- **A credential in a URL query string is now redacted.** Masking matched on key *names* only,
  while the CLI itself bakes credentials into the endpoint (`--api-key ...:in=query`, a Gemini-style
  `?key=`). The credential therefore survived redaction in a *value*: printed by `adapter show`
  while it claimed "secrets masked", logged on every probe, written to capture transcripts, and
  posted to the platform inside a failing probe's error text. Redaction is value-aware now, and the
  adapter scrubs the URL before logging or reporting it.

### Fixed
- **A create-then-stream target is now built correctly from captured evidence.** compose() picks
  one branch by transport and the streaming branch never consulted the session layer — only
  `direct_api` got a "session upgrade". So an agent that makes you create a conversation before
  streaming (create a thread, then stream the turn) was composed as a plain POST to the captured
  path, which still contained the conversation id from the capture: every probe posted into one
  dead conversation, and the create step vanished even though it had been detected. The `create`
  block is now emitted and the path is templated with `{{CONV}}`.
- **The prompt is substituted into the create call.** These APIs routinely name the conversation
  after the question (`{"description": "{{PROMPT}}"}`); only `{{CONV}}` was substituted, so the
  literal placeholder was posted as the title.
- **Progress chatter no longer arrives as the agent's answer.** A stream can type its frames on
  the SSE `event:` line rather than a field in the payload. Those payloads have no `type`, so the
  frame filter fell through and collected EVERY frame — prepending "Analyzing query…",
  "Searching resources…" to every reply, which the scorer then reads as the agent's words. The
  event name is now used as the frame type, and only when `token_types` is explicitly configured,
  so a stream relying on the collect-everything default is unaffected.
- **The streaming field mapping is derived from captured evidence** instead of emitting a bare
  `{"format": "sse"}` that collects no frames. Derivation is event-aware on purpose: picking the
  field that appears most often selects the progress chatter, because status frames outnumber
  answer frames.
- **`--login-url` records a repeatable login, not just the token it produced.** Its own docstring
  claimed it returned "an `auth` block so the bridge re-authenticates on its own during a long
  run"; the code returned only a header, so the config carried a static token that died with it.
  It now writes a `derived_multihop` auth block plus `reauth_on_401`, and credentials written as
  `env:NAME` stay out of the config file.
- Tests that pin a config directory now clear `ASCEND_CONFIG_DIR` as well as the legacy name, so
  an ambient value in the developer's shell no longer causes spurious failures.
- **A streaming target with a query string no longer loses it, or forks a config on every run.**
  Promoting a config to `sse_stream` split the endpoint into `base_url` + `chat_path` and dropped
  the query, while the probe path deliberately keeps it. Where the query is *required* — Azure
  OpenAI's `?api-version=`, Vertex's `?alt=sse` — the upgraded config called a URL the target does
  not serve, so the re-validation failed and the streaming upgrade silently never applied, leaving
  a `direct_api` config that hands the scorer raw `data:` frames. Where it was optional, the stored
  endpoint no longer matched what the next run derived, so an ordinary re-run looked like a
  different target: the freshly captured credential was written to `<name>-2` while `--config
  <name>` kept serving the expired one.
- **A second bot on the same host no longer destroys the first one's config.** The config name is
  derived from the URL's *host*, so two endpoints on one host (`https://h/chat` and
  `https://h/v1/chat`) derived the same filename and the second run overwrote the first — including
  the `_ascend` app binding it carried — and exited 0 with a success message. A genuinely different
  endpoint under an already-used name is now saved alongside as `<name>-2`, with both targets named
  in the output.
- **Re-deriving a config no longer unbinds it from its application.** A refresh rewrote the file
  wholesale, discarding the `_ascend` binding written at registration, so the target silently lost
  its app. Binding metadata is now carried forward.
- **An update rewrites the file it resolved from**, instead of writing to whichever config
  directory the current working directory happened to select — which produced a second copy
  elsewhere rather than updating the one in use. Writes stay inside a real config directory: reads
  deliberately search wider (the working directory, and a frozen build's unpacked examples), and a
  write must never follow them there.

- `--out ./mybot.json` now writes to the current directory. `Path("./x").parent == Path(".")`, so an
  explicitly written path was indistinguishable from a bare name and the file appeared in the config
  directory instead.
- `--out out/mybot` now writes `out/mybot.json`. The extension was only added for bare names, so
  this wrote a file literally named `mybot` — which nothing that looks for `*.json` can ever pick
  up, including `adapter configs` and name-based `--config` resolution. (A config written outside a
  config directory is still reached by path, not by bare name: `--config out/mybot.json`.)
- **`--out` pointing at a directory is now a usage error** instead of silently writing a file named
  after that directory (`--out out/` wrote `./out.json`), or crashing with a raw pathlib
  `ValueError` after the probe and the live validation had already run (`--out ./`).
- `--code` honours the directory in `--out` instead of reducing it to a stem and writing to the
  config dir regardless — which is what the docs already promised. When the module lands outside
  the config dir its pointer records an absolute path, because the `custom` adapter looks only in
  the config dir and would otherwise fail to load a module that had just validated.

- **A config now resolves the same way from every directory.** Config lookup picked the first
  configs *directory* that existed and then searched only inside it. Every checkout of this repo
  ships a `configs/` of examples, so running the CLI from a checkout made `~/.ascend/configs`
  invisible: a target created from one directory was "config not found" from another, and after
  upgrading by re-installing, a working target could disappear entirely. What the operator saw was
  a bridge — because `runtime start` exits before it ever leases, and a relay that never starts is
  indistinguishable from one that dropped. The app's *key* kept resolving throughout (keys live in
  `~/.ascend` and never depended on the working directory), which is exactly what made it read as
  a flaky bridge rather than a lookup bug. Configs are now searched per *file* across every config
  directory, `adapter configs` lists all of them and says where new ones are written, and precedence
  is unchanged — an explicit `$ASCEND_CONFIG_DIR` still wins, and a local `configs/` still shadows
  home, so nothing that resolves today resolves differently.

### Added
- **`--app <name|aapp_id>` on `target add` / `onboard`: bind to an application that already
  exists** instead of creating a second one, fetching its bridge key for you. This is the shape of
  a stalled engagement — the app was configured in the Console (system prompt, controls, size,
  QPM), someone starts an assessment, it fails, and nobody can say where the bridge is. There is
  nothing to find: a bridge is a process this CLI runs. Creating a fresh app instead stranded all
  of that configuration on an application nobody assesses.
- **`configs/example-create-then-stream.json`: a complete target definition** for the hardest
  common shape — bearer auth, a conversation that must be created first, and an answer streamed
  back as named SSE events interleaved with progress chatter. Every section is annotated with what
  it controls and how it fails. Copy it, change the ALL-CAPS values, `adapter validate` it: a
  deterministic path that needs no capture and no inference.
- **`--save-as <name>` on `target add` / `onboard`.** The config name was derived from the URL and
  never choosable, so it came out as `myhost-com` or `127-0-0-1-8791` and the only way to learn it
  was to read a line of stderr — then you had to pass exactly that to `--config` later. Name it
  once and every later step is deterministic.

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

- **The MCP shim can onboard a target.** It exposed nine tools that could run and read an
  assessment but could not *create* the thing being assessed, so an agent driving Ascend over MCP
  hit a wall at the first step and had to drop to a shell. `ascend_target_add` and
  `ascend_target_check` close that gap, and lead the tool list because an agent reads it top-down.

- `adapter validate` reports the target's measured reply time and warns when it cannot survive the
  platform's per-probe window — a config can be correct and the target still unassessable. Learned
  from one probe instead of from a whole failed run.
- `$ASCEND_PLATFORM_PROBE_WINDOW_MS` sets the per-probe window the CLI assumes the platform
  enforces. It is the only timeout knob: the bridge's give-up point and the adapter's own timeout
  are derived from it, so raising the platform-side window is a config change, not a release.

### Changed
- The skills carry a troubleshooting playbook and a per-target-pattern catalog: which adapter
  suits which target shape, and the way each one specifically fails. Most failures here present
  as a different failure than they are, so the playbook is ordered by symptom and starts with the
  one number that settles it — `ANS` in `bridge ls`.
- `ascend --help` leads with the one command that does the whole flow (`onboard`) and shows the
  seven you use day to day, with the rest listed by name. 71 lines to 44. No command changed or
  moved — every one still runs exactly as before.

### Compatibility
Nothing that works today changes. Specifically, and covered by tests:
- a bare `--out <name>` still lands in the config dir, so `--config <name>` still finds it;
- an absolute `--out` path is untouched;
- re-running against the **same** endpoint still overwrites the config in place — that is an
  intentional refresh and scripts depend on it. Only a *different* target under a used name is
  moved aside, and only when the name was derived rather than given;
- `--save-as` is explicit intent and overwrites deliberately;
- no existing file is moved, renamed or migrated.

### Documentation
- **The shipped docs and the architecture diagrams now describe the tool as it is.** They still
  taught the old shape — onboarding framed as picking among source flags, `target` absent
  everywhere, and the interactive map's lifecycle stepping through build → register → assess. The
  README, architecture, lifecycle, surface, usage and adapter guides now lead with `target`, and
  the interactive map's lifecycle is `identify → add target → assess → analyze`, with `app`,
  `adapter` and `keys` documented as the machinery underneath rather than as the way in.
- Corrected while auditing them, since a wrong reference is worse than a missing one: the adapter
  count was documented as 13 or 14 in five places and is **15**; the stateful-adapter set was
  documented as 8 and is **12**; `terminal` was listed as a transport and does not exist; the
  usage guide claimed the `app` verbs only covered bridge apps and sent readers to a Python
  snippet, when `app create --type` has covered all four types for some time; the README claimed
  45 commands against a generated reference that counts 53; and the architecture diagram gave the
  cloud lease service and your local bridge process the same node id, so they rendered as one box
  with a self-loop. The two are now named separately, in the glossary as well.
- Documented the per-probe window (~110–120s, timed from when a probe is **queued**) and the fact
  that exceeding it returns a synthetic timeout indistinguishable from a target failure — the
  single most misread behaviour in this system, because it presents as a dropped bridge.

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

---
name: build-adapter
description: >-
  Turn captured target traffic (a HAR export, a browser in-page capture, or a
  proxied send) into a validated Ascend adapter config. Drives the deterministic
  `ascend adapter build` classifiers, resolves only the low-confidence layers with
  judgment, and gates on `ascend adapter validate` against the live target. Use
  when onboarding a new red-team target whose transport/auth/session shape is not
  already a known preset, or when an existing config stops matching the target.
---

# build-adapter

An adapter is **not** a class you pick from a list. It is a **composition of six
orthogonal layers**, each with a finite set of values. This skill is the reasoning
wrapper around a deterministic pipeline: the CLI classifies each layer from
evidence, you resolve only the ambiguous residue, and the CLI **validates** the
composition against the live target before anything ships. Determinism lives in
the CLI; you supply judgment only where a classifier reports low confidence.

Read `docs/CAPABILITY_MATRIX.md` (the full contract) before starting. `ascend
adapter layers` prints it. This skill assumes it.

> **Hard rule: never ship an unvalidated config.** A config that has not passed
> `ascend adapter validate` (replay of the captured turn *and* a fresh probe,
> compared to the observed answer) is not done. A plausible-looking config that
> silently mis-parses the target produces a whole assessment of garbage results.

`ascend` below means `python3 shells/cli/ascend.py`.

## The six layers (what you are composing)

One value per layer, plus that value's params:

1. **Transport & assembly** — `rest_json` · `sse` · `ndjson` · `websocket` ·
   `poll` · `browser_dom` · `terminal`. *How a response is carried and reassembled.*
2. **Auth** — `none` · `static` · `mtls` · `derived_multihop` · `oauth2` · `csrf`.
   *How one request is authorized.*
3. **Auth lifecycle** — `static` · `refresh_on_ttl` · `reauth_on_401` ·
   `cookie_rotation`. *How the credential stays valid over a long run.* This one is
   declared with an `auth` block and is the usual cause of a run that dies half way —
   see **Layer 3 in practice** below.
4. **Session / conversation** — `stateless` · `create_session` ·
   `create_conversation` · `warmup` · `multi_turn`. *How turns bind together.*
5. **Identity** — `fixed` · `rotate_per_conversation` · `rotate_per_n` ·
   `fresh_per_probe`. *Who is calling (mostly an ROE choice, not auto-detected).*
6. **Rate / concurrency** — `qpm`, `max_workers`, `per_identity_qpm`. Cross-cutting;
   `max_workers` auto-defaults to 1 for stateful, 10 for stateless.

## Layer 3 in practice: keeping a credential alive for a whole run

This is the single most common reason an onboarded target stops working part-way through
an assessment, and it does not look like an auth problem — it looks like a well-behaved
bot that refuses everything.

A token captured at build time (a mobile app's bearer, an OAuth access token, a login
cookie) is valid when you build the adapter and expired an hour into the run. Every probe
after expiry gets a 401, the adapter reports a failure, the scorer sees "no answer", and
the run finishes **looking clean while measuring nothing**. Worse: when probes keep
failing the platform **auto-pauses the assessment**, so the visible symptom is a stalled
run and an idle bridge — which reads as "the bridge died".

Declare it instead of pasting a token. Two separate blocks, because they answer two
different questions — **`auth`** is *who mints the credential* (Layer 2), and
**`auth_lifecycle`** is *when to re-acquire it* (Layer 3):

```json
"auth": {
  "type": "oauth2",
  "grant": "client_credentials",
  "token_url": "https://api.example.com/oauth/token",
  "client_id_ref": "env:MYBOT_CLIENT_ID",
  "client_secret_ref": "env:MYBOT_CLIENT_SECRET"
},
"auth_lifecycle": {
  "type": "reauth_on_401"
}
```

`grant` is `client_credentials` | `password` | `refresh` — use `refresh` with
`refresh_token_ref` for a mobile-style refresh-token flow. Other `auth.type` values are
`static` (bearer / api_key / basic / cookie), `mtls` and `csrf`.

> Secrets are `env:NAME` references, never inline literals — a config carrying a real token
> is refused. That is deliberate: configs get committed, pasted into tickets and shared.

| `auth_lifecycle.type` | use when | behaviour |
|---|---|---|
| `static` (default) | the credential outlives any run | never re-acquired |
| `refresh_on_ttl` | the token has a known TTL, or is a JWT | re-mints once `ttl_s` elapses, or when the JWT `exp` is within `skew_s` |
| `reauth_on_401` | expiry is unpredictable, or revocation happens | on a challenge status (default 401) re-acquires and retries the probe **once** |
| `cookie_rotation` | session-cookie targets | re-acquires on `interval_s` and whenever a response sets a new cookie |

This works for **every** adapter, because it is applied at the shared call seam rather than
inside individual adapters. `agentforce`, `copilot_studio` and `amazon_connect` additionally
mint and re-mint their own vendor credentials, so they need no `auth` block at all.

An `oauth2` config with no `auth_lifecycle` still refreshes on a fixed TTL, which is the
long-standing behaviour — but if the target's token is shorter-lived than that, say so
explicitly with `reauth_on_401`, or every probe between expiry and the next refresh is
scored as a refusal.

## Timeouts: never pin a value sized for a fast bot

Agentic targets routinely take **2-3 minutes** per reply and some take considerably
longer. A short timeout does not degrade gracefully — it converts a healthy slow target
into 100% probe failures, which then trips the platform's auto-pause. Measured live: a
110s target under a 20s config timeout failed *every* probe.

Only pin `timeout_ms` when you have measured the target and want to *cap* it. Otherwise
leave it out and let the runtime default apply (it sits above the ~10 minute envelope), and tune
per-environment with `$ASCEND_TARGET_TIMEOUT_MS`. A ceiling
(`$ASCEND_TARGET_MAX_TIMEOUT_MS`) still applies so one hung target cannot hold a worker
open for the whole run — slow is fine, hung is not.

### The ceiling you cannot configure away

The adapter's `timeout_ms` is not the binding constraint. **The platform gives a bridge a
bounded window to return a result** — `probe_shadow`'s `BRIDGE_RESPONSE_TIMEOUT`, on the
order of **100-120s** — and the bridge deliberately gives up just under it
(`_DEFAULT_BRIDGE_RESPONSE_TIMEOUT_S`, 110s) rather than hold a worker open for a result
nobody will accept.

So a target that reliably takes **longer than ~110s cannot be assessed through the bridge
today**, no matter how large you make `timeout_ms`. That is a platform-side limit, not an
adapter bug. When you hit it:

- Do **not** keep raising `timeout_ms` — through the bridge it changes nothing.
- Say so explicitly rather than reporting the target as failing its probes.
- The platform window has to be raised first; then match it with
  `$ASCEND_BRIDGE_RESPONSE_TIMEOUT_MS` (or `bridge_response_timeout_ms` in the config), and tell
  the CLI what the new window is with `$ASCEND_PLATFORM_PROBE_WINDOW_MS`.

`ascend adapter validate` checks this for you: it prints the measured reply time and warns when the
target is at or beyond the window (unassessable) or close enough that queueing alone can blow it.
Treat that warning as a stop sign — the config being green does not mean the target can be run.

One subtlety worth knowing: the probe's clock starts when the platform **queues** it, not when the
bridge calls the target. A target comfortably under the window can still time out while waiting to
be leased, which is why QPM and `max_workers` matter for slow targets beyond simple politeness.

`timeout_ms` still matters everywhere else — `adapter validate`, `chat`, and any target
*under* the window — which is where a pinned 20-30s value silently fails everything.

## Mobile apps

You do not red-team the app binary; you red-team the **backend it calls**. Capture the
app's traffic (a proxy with the device trusting its CA, or a HAR from the web equivalent),
then build an adapter against that API exactly as for any other HTTP target. Mobile
backends are also the most likely place to need Layer 3 above: their tokens are usually
short-lived and refresh-token based.

## Workflow

### 1. Gather evidence
Get at least one **real answered turn** from the target, captured end to end:

- **HAR** — export from browser devtools (Network → Save all as HAR). Best when a
  login precedes the chat call (reveals L2 `derived_multihop` / L3 lifecycle).
- **Browser in-page capture** — when the target is only reachable inside a page
  (SPA, widget). Yields L1 `browser_dom` or an intercepted fetch/xhr.
- **Proxied send** — a single request you drove through a proxy.

You need the request(s) *and* the target's actual answer text, so validation has
a ground truth to compare against.

### 2. Run the deterministic classifiers
```
ascend adapter build --har <evidence.har>            # human summary
ascend adapter build --har <evidence.har> --json     # per-layer {value, params, confidence, evidence}
```
The output is one classification per layer: the chosen `value`, its `params`, a
`confidence`, and the `evidence` (which frames/headers drove the pick). Read it as
a draft config plus a confidence map — **not** a finished answer.

> If `discover` is unavailable in your build (it is the composable-layers phase; a
> scaffold exits non-zero pointing at the matrix), fall back to composing the
> config by hand from `docs/CAPABILITY_MATRIX.md` using the detect-by column for
> each layer, then jump straight to step 4 (validate). The validation gate is the
> real contract; discovery is an accelerant, not a substitute.

### 3. Resolve the low-confidence layers (this is the judgment step)
For every layer the classifier flagged low-confidence, decide with the evidence in
front of you. Do **not** guess blindly — pick the alternate that the evidence
supports and let validation arbitrate. The recurring hard cases:

- **WebSocket: chunked text vs JSON framing.** Try `json.loads` on each frame. If
  every frame parses as JSON → `framing: json` with a `response_path` into the
  frame. If frames are raw text fragments to concatenate → `framing: text` with
  `aggregate: concat`. Getting this wrong yields either dropped tokens or a stream
  that never assembles. Decide `done_when` (a sentinel/terminal frame) vs `idle_ms`
  (quiet-period close) the same way — prefer an explicit sentinel if one exists.
- **Multi-step create-conversation.** If a response **id reappears in a later
  request's URL or body** (id-flow), it is L4 `create_conversation`: capture the
  `create_req -> conversation_id`, then send to `/conversations/{id}/messages`.
  Distinguish from `create_session` (a session id used by sends but no per-message
  path) and from `warmup` (a mandatory greeting/consent turn whose first reply you
  discard).
- **Cookie / token re-auth.** If a login request **precedes** the chat request and
  its response value reappears downstream → L2 `derived_multihop` (chain the
  extract into the send). If `Set-Cookie` churns or the session dies after an
  interval → L3 `cookie_rotation`. If you observed a re-login after a 401/403 →
  L3 `reauth_on_401`. If the token carries an `exp` / documented TTL →
  `refresh_on_ttl`.
- **Rotating identity.** Mostly an ROE decision, not a wire signal. Choose
  `rotate_per_conversation` / `fresh_per_probe` when the target rate-limits or
  tracks per-user and you need probe isolation; supply the `identity_pool`. Default
  `fixed` unless there is a reason.
- **Rate / concurrency.** Leave `max_workers` on its auto-default (1 stateful, 10
  stateless) unless the target is fragile; set `qpm` to the agreed ROE cap.

### 4. Validate — the hard gate
Replay the captured turn **and** a fresh probe through the composed config against
the live target and compare to the observed answer:
```
ascend adapter validate --config <config> --json
```
Green (both replay and fresh probe match the observed answer) → the config is
shippable. Anything else → not done.

> Fallback when the `validate` verb is a scaffold in your build: validate by a live
> single-probe relay. Create a throwaway thin app, start the runtime against your
> config, and confirm one probe returns the target's real answer:
> `ascend app create --type bridge --name probe-check` →
> `STRAIKER_BRIDGE_API_KEY=<tc-key> ascend runtime start --adapter <type> --config <config> --qpm 2`
> then delete the throwaway app. A config that cannot return one correct live
> answer is not validated.

### 5. Iterate the failing layer, not the whole config
On mismatch, the classifier's confidence map tells you which layer to suspect
first. Change **one** layer's value/params to its evidence-supported alternate
(WS `json`↔`text`, `done_when`↔`idle_ms`, `create_conversation`↔`create_session`,
add a `warmup`, add L3 `reauth_on_401`), then re-validate. Repeat until green. Do
not stack speculative changes — one variable at a time keeps the signal clean.

### 6. Confirm and hand off
```
ascend adapter show <config>      # inspect the final composition
ascend adapter list               # confirm the transport/preset type resolves
```
Only a green-validated config proceeds to `onboard-target` / `assess run`. If you
genuinely cannot get a layer green, emit the low-confidence discover report plus
the raw evidence and escalate that specific layer — do **not** ship a guess.

## Symptom -> cause

Read this before re-building an adapter. Most "the adapter broke" reports are one of
these, and only the last one is actually a transport problem.

| symptom | almost always | fix |
|---|---|---|
| `answered=0` with `failed` climbing in `bridge ls` | the adapter is failing every probe — timeout too short, or the credential expired | raise/remove `timeout_ms`; add an `auth` block |
| worked for the first N probes, then everything "refuses" | short-lived credential expired mid-run | `reauth_on_401` / `refresh_on_ttl` |
| assessment sits at `paused` and nothing moves | the platform auto-paused after repeated probe failures | fix the adapter failure, then `assess resume` |
| every probe fails against a slow/agentic target | `timeout_ms` shorter than the target's reply time | remove the pin or raise it; agents take 2-3 min+ |
| a run scores perfectly clean and suspiciously fast | probes went unanswered — a **false pass**, not a good result | confirm `answered > 0` before believing any score |
| replies are truncated or arrive as fragments | transport/assembly (L1) is wrong — streaming read as REST | re-check `sse`/`ndjson`/`websocket` framing |

## Definition of done
- Every layer has a value; every low-confidence layer was resolved with evidence.
- `ascend adapter validate` (or the live single-probe fallback) is **green** on
  both the replayed turn and a fresh probe.
- `qpm` / identity honor the ROE.
- No unvalidated config left behind.

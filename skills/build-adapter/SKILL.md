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
   `cookie_rotation`. *How the credential stays valid over a long run.*
4. **Session / conversation** — `stateless` · `create_session` ·
   `create_conversation` · `warmup` · `multi_turn`. *How turns bind together.*
5. **Identity** — `fixed` · `rotate_per_conversation` · `rotate_per_n` ·
   `fresh_per_probe`. *Who is calling (mostly an ROE choice, not auto-detected).*
6. **Rate / concurrency** — `qpm`, `max_workers`, `per_identity_qpm`. Cross-cutting;
   `max_workers` auto-defaults to 1 for stateful, 10 for stateless.

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

## Definition of done
- Every layer has a value; every low-confidence layer was resolved with evidence.
- `ascend adapter validate` (or the live single-probe fallback) is **green** on
  both the replayed turn and a fresh probe.
- `qpm` / identity honor the ROE.
- No unvalidated config left behind.

# Caching and performance

Two changes reduced per-command latency:

| Cost | Before | Now |
|---|---|---|
| PAT→JWT exchange, **every command** | 1.14s | 0s while a cached token is valid |
| HTTP connections | a fresh TCP+TLS handshake per call | one pooled connection (~64% faster over 6 calls) |
| `ascend controls list` (end to end) | 1.54s | **0.41s** |
| `ascend app list` | ~1.5s | **0.42s** |

## The JWT cache

A platform PAT is exchanged for a short-lived JWT (**~10 minutes**). That token is cached at
`<state-dir>/jwt.json`, mode **0600**, and reused until 60s before it expires.

Safety properties:
- keyed by `sha256(PAT | token_url)`; the PAT itself is never written;
- **tenant-scoped**, and the cached token's own `iss|straikerId` fingerprint must match the pinned
  tenant before it is used, so a token can never leak across tenants;
- dropped on a 401 (so a rejected token isn't re-read by the next process) and on `tenant switch`;
- a token whose `exp` cannot be decoded is kept briefly in memory but **never** persisted.

The long-lived PAT already sits in the environment; the cached JWT is a 10-minute credential in a
0600 file.

## What is never cached

- **`get_assessment`**. It is the live status; caching it would freeze `assess watch` and the
  poller.
- **The assessments list on any liveness path**. A stale `complete` would hide a running
  assessment whose relay died, which is how a false pass happens. `assess run`
  auto-manages the bridge, so this is now mainly a risk when auto-management is off or a remote
  bridge dies. The liveness path stays uncached so `bridge sync` and the NO-BRIDGE alarm see current state.
- Anything non-GET, and any response carrying a one-shot `tc-` key.

`ASCEND_NO_CACHE=1` disables caching entirely.

## Operations spanning all apps

There is no tenant-wide assessments endpoint, so anything spanning apps (`app list --with-runs`,
`reports`, `status`, `relay ls`'s orphan check) is **one call per app**, run 12-wide in parallel with
a progress line. On a 38-app tenant that is ~2s. A spinner shows progress during the scan.

Shortcuts when you don't need the scan:
```
ascend status --quick          # skip the per-app assessment scan
ascend app list                # apps only, no runs (~0.4s)
ascend bridge ls --no-check     # local bridge state only, no tenant lookup
ascend reports                 # cheap columns; --detail adds a call per run
```

## Retries and pooled connections

Pooled keep-alive sockets go stale when the server closes an idle connection, which surfaces as
`RemoteDisconnected`. Idempotent methods (GET/HEAD/OPTIONS) retry automatically. **POSTs do not**,
because replaying one could create a second app or assessment. When a create hits a transport
error the CLI **verifies against the server** before reporting, so a create that actually succeeded
is not reported as failed.

## The platform's per-probe window

The CLI's own latency is small next to the one limit that decides whether a target can be assessed
at all. The platform gives each probe a bounded window — **~120s** — and the clock starts when the
probe is **queued**, not when the bridge calls the target. A probe can spend much of its budget
waiting to be leased.

Blowing the window is not a slow probe. It surfaces as a synthetic timeout indistinguishable from
the target failing, which feeds the platform's target-health streak and **auto-pauses the
assessment**. A target that reliably answers past the window therefore produces a whole run of
false failures that reads as a broken bridge. Agentic targets taking 2–3 minutes per turn are
common and are past it. Raising the adapter timeout does not help — the window has to be raised on
the platform side first, and then `$ASCEND_PLATFORM_PROBE_WINDOW_MS` tells the CLI about it.

### One number, two derived values

`PLATFORM_PROBE_WINDOW_S` in `runtime/adapters/base.py` is the only knob. Three settings for one
quantity is three ways to set them inconsistently, so the rest are derived from it:

| Value | Derivation | Default |
|---|---|---|
| platform per-probe window | `$ASCEND_PLATFORM_PROBE_WINDOW_MS`, else the built-in | **120s** |
| bridge give-up — the router abandons the probe | window − 10s delivery margin | 110s |
| adapter timeout when the config sets no `timeout_ms` | give-up − 10s handler margin | 100s |

A config's `timeout_ms` still wins, but it is **clamped to the bridge give-up point**. Waiting past
it cannot help: the router has already abandoned the probe, and the extra time only holds a worker
and a socket open.

### Learn it from one probe, not from a failed run

`adapter validate` times the target and warns on that measurement. `target check` is the same gate,
resolved from a target name instead of a config name:

```
ascend target check mybot
ascend adapter validate --config mybot
```

Both print the measured reply time alongside the result, and warn on two thresholds:

- **at or beyond the window** — every probe times out platform-side, the assessment auto-pauses,
  and the run reports no findings having measured nothing;
- **at 60% of the window or more** — the target is inside it, but queue wait can still push a probe
  past it. Keep QPM and `max_workers` low, and read sporadic failures as this rather than as target
  refusals.

### Adapters that do not derive their timeout

Most adapters call `resolve_timeout_s(config)` and inherit the derivation above. Five carry their
own ceiling instead:

| Adapter | Its own limit |
|---|---|
| `slack_direct` | `timeout_ms`, default 90000 |
| `scrt2_direct` | `sse_timeout`, default 45s |
| `browser` | per-step Playwright timeouts (`response.timeout_ms`, default 30000) |
| `bedrock` | none applied — boto3 client defaults (`timeout_ms` is documented but not read) |
| `custom` | whatever the module does |

The router abandons any probe at the bridge give-up point regardless, so none of these can exceed
the window — they can only give up earlier than it.

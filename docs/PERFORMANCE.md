# Speed: what's cached, and what never is

The CLI was slower than it needed to be for two measurable reasons, both fixed:

| Cost | Before | Now |
|---|---|---|
| PAT→JWT exchange, **every command** | 1.14s | 0s while a cached token is valid |
| HTTP connections | a fresh TCP+TLS handshake per call | one pooled connection (~64% faster over 6 calls) |
| `ascend controls list` (end to end) | 1.54s | **0.41s** |
| `ascend app list` | ~1.5s | **0.42s** |

## The JWT cache

A platform PAT is exchanged for a short-lived JWT (**~10 minutes** — an older comment claimed an
hour). That token is now cached at `<state-dir>/jwt.json`, mode **0600**, and reused until 60s
before it expires.

Safety properties, because this is a bearer token on disk:
- keyed by `sha256(PAT | token_url)` — the PAT itself is never written;
- **tenant-scoped**, and the cached token's own `iss|straikerId` fingerprint must match the pinned
  tenant before it is used, so a token can never leak across tenants;
- dropped on a 401 (so a rejected token isn't re-read by the next process) and on `tenant switch`;
- a token whose `exp` cannot be decoded is kept briefly in memory but **never** persisted.

It is strictly less exposure than the status quo: your long-lived PAT already sits in the
environment; this is a 10-minute credential in a 0600 file.

## What is never cached

- **`get_assessment`** — it *is* the live status. Caching it would freeze `assess watch` and the
  poller.
- **The assessments list on any liveness path** — a stale `complete` would hide a running
  assessment whose relay died, which is precisely how a false pass happens. `assess run`
  auto-manages the bridge, so this is now mainly a risk when auto-management is off or a remote
  bridge dies; the liveness path stays uncached so `bridge sync` and the NO-BRIDGE alarm see truth.
- Anything non-GET, and any response carrying a one-shot `tc-` key.

`ASCEND_NO_CACHE=1` disables caching entirely.

## Why "across all apps" is still a fan-out

There is no tenant-wide assessments endpoint, so anything spanning apps (`app list --with-runs`,
`reports`, `status`, `relay ls`'s orphan check) is **one call per app**, run 12-wide in parallel with
a progress line. On a 38-app tenant that is ~2s, not instant — and the spinner exists so you can
see it working rather than guess.

Shortcuts when you don't need the scan:
```
ascend status --quick          # skip the per-app assessment scan
ascend app list                # apps only, no runs (~0.4s)
ascend bridge ls --no-check     # local bridge state only, no tenant lookup
ascend reports                 # cheap columns; --detail adds a call per run
```

## Retries and pooled connections

Pooled keep-alive sockets go stale when the server closes an idle connection, which surfaces as
`RemoteDisconnected`. Idempotent methods (GET/HEAD/OPTIONS) retry automatically. **POSTs do not** —
replaying one could create a second app or assessment. Instead, when a create hits a transport
error the CLI **verifies against the server** before reporting, so you never get "failed" for
something that actually succeeded (which would invite a destructive retry).

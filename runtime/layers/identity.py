"""
layers.identity — Layer 5 (Identity): *who* is calling the target.

An assessment sends many probes. Depending on the engagement and
how the target tracks callers, each probe may want to present as the same user,
a rotating cast of users, or a fresh throwaway user every time. This module
turns an ``identity`` config block into a deterministic function that hands the
right identity variables (e.g. ``username`` / ``email`` / ``token``) to any
given conversation key or probe index.

Everything here is **pure and deterministic** — no network, no clocks, no
global mutable state that changes the answer for the same inputs. That makes the
selection reproducible across a run and trivially unit-testable.

Config shape (lives under ``config["identity"]``)::

    {
      "mode": "fixed" | "rotate_per_conversation" | "rotate_per_n" | "fresh_per_probe",
      "pool": [ {"username": "u1", "email": "u1@x", "token": "env:U1_TOKEN"}, ... ],
      "n": 25,                       # rotate_per_n only: probes per identity
      "template": {                  # fresh_per_probe only, when no pool given
          "username": "redteam_{{N}}",
          "email": "redteam_{{N}}@example.test"
      }
    }

Modes
-----
``fixed``
    Always the first pool entry (or ``{}`` if the pool is empty). The default.
``rotate_per_conversation``
    Each distinct conversation key maps to a pool entry. The mapping is a stable
    hash of the key modulo the pool size, so the *same* conversation always gets
    the *same* identity within and across runs, while different conversations
    spread across the pool.
``rotate_per_n``
    Advance one pool entry every ``n`` probes: ``pool[(index // n) % len(pool)]``.
``fresh_per_probe``
    A unique identity per probe. If a ``pool`` is supplied it is indexed by
    ``index % len(pool)``; otherwise identities are *generated* from ``template``
    by substituting ``{{N}}`` with the probe index (still fully deterministic).

Token/secret fields are left as references (e.g. ``"env:U1_TOKEN"``); this layer
never resolves them — :mod:`layers.auth` owns secret resolution. Identity only
decides *which* identity applies.
"""
from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Optional

VALID_MODES = ("fixed", "rotate_per_conversation", "rotate_per_n", "fresh_per_probe")


class IdentityError(ValueError):
    """Raised for a malformed identity config (bad mode, missing pool, etc.)."""


def _stable_index(key: str, modulus: int) -> int:
    """Deterministic, run-stable index for ``key`` in ``[0, modulus)``.

    Uses SHA-256 rather than the built-in :func:`hash` so the mapping is stable
    across processes (Python randomizes ``str`` hashing per-process).
    """
    if modulus <= 0:
        return 0
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % modulus


def _render_template(template: Dict[str, Any], index: int) -> Dict[str, Any]:
    """Build a generated identity by substituting ``{{N}}`` -> ``index``."""
    out: Dict[str, Any] = {}
    for k, v in template.items():
        out[k] = v.replace("{{N}}", str(index)) if isinstance(v, str) else v
    return out


def resolve_identity(
    id_config: Optional[Dict[str, Any]],
    *,
    conv_key: Optional[str] = None,
    probe_index: int = 0,
) -> Dict[str, Any]:
    """Pure resolver: return the identity vars for one probe/conversation.

    This is the single source of truth for the selection logic;
    :class:`IdentityManager` is a thin convenience wrapper around it.

    Args:
        id_config: the ``identity`` block (``None``/``{}`` -> ``fixed`` w/ empty pool).
        conv_key: conversation key (used by ``rotate_per_conversation``).
        probe_index: zero-based probe ordinal (used by the ``rotate_per_n`` and
            ``fresh_per_probe`` modes).

    Returns:
        A dict of identity variables (a copy; callers may mutate it freely).
    """
    cfg = id_config or {}
    mode = cfg.get("mode", "fixed")
    if mode not in VALID_MODES:
        raise IdentityError(f"unknown identity mode {mode!r}; valid={VALID_MODES}")

    pool: List[Dict[str, Any]] = list(cfg.get("pool") or [])

    if mode == "fixed":
        return dict(pool[0]) if pool else {}

    if mode == "rotate_per_conversation":
        if not pool:
            raise IdentityError("rotate_per_conversation requires a non-empty 'pool'")
        key = conv_key if conv_key is not None else "default"
        return dict(pool[_stable_index(str(key), len(pool))])

    if mode == "rotate_per_n":
        if not pool:
            raise IdentityError("rotate_per_n requires a non-empty 'pool'")
        n = int(cfg.get("n", 1) or 1)
        if n < 1:
            raise IdentityError("rotate_per_n 'n' must be >= 1")
        return dict(pool[(probe_index // n) % len(pool)])

    # fresh_per_probe
    if pool:
        return dict(pool[probe_index % len(pool)])
    template = cfg.get("template")
    if template:
        return _render_template(template, probe_index)
    # No pool and no template: still deterministic, still "fresh" per probe.
    return {"identity_index": probe_index}


class IdentityManager:
    """Deterministic Layer-5 selector built from an ``identity`` config block.

    Stateless with respect to the answer it gives: calling
    :meth:`resolve` / :meth:`for_conversation` / :meth:`for_probe` with the same
    arguments always yields the same identity. It is safe to share one instance
    across threads.

    Example::

        mgr = IdentityManager.from_config(config)
        ident = mgr.for_probe(7)          # -> {"username": ...}
        ident = mgr.for_conversation("c1")
    """

    def __init__(self, id_config: Optional[Dict[str, Any]]) -> None:
        cfg = id_config or {}
        self.mode: str = cfg.get("mode", "fixed")
        if self.mode not in VALID_MODES:
            raise IdentityError(f"unknown identity mode {self.mode!r}; valid={VALID_MODES}")
        self.pool: List[Dict[str, Any]] = list(cfg.get("pool") or [])
        self.n: int = int(cfg.get("n", 1) or 1)
        self.template: Optional[Dict[str, Any]] = cfg.get("template")
        self._config = cfg
        # Validate eagerly so misconfig surfaces at build time, not mid-run.
        if self.mode in ("rotate_per_conversation", "rotate_per_n") and not self.pool:
            raise IdentityError(f"identity mode {self.mode!r} requires a non-empty 'pool'")
        if self.mode == "rotate_per_n" and self.n < 1:
            raise IdentityError("rotate_per_n 'n' must be >= 1")

    @classmethod
    def from_config(cls, config: Optional[Dict[str, Any]]) -> "IdentityManager":
        """Build from a *full* adapter config (reads its ``identity`` block)."""
        block = (config or {}).get("identity") if config else None
        # Accept being handed the identity block directly, too.
        if block is None and config is not None and "mode" in (config or {}):
            block = config
        return cls(block)

    def resolve(self, *, conv_key: Optional[str] = None, probe_index: int = 0) -> Dict[str, Any]:
        """Return identity vars for a probe/conversation (see :func:`resolve_identity`)."""
        return resolve_identity(self._config, conv_key=conv_key, probe_index=probe_index)

    def for_conversation(self, conv_key: Optional[str]) -> Dict[str, Any]:
        """Identity for a whole conversation (index 0 within it)."""
        return self.resolve(conv_key=conv_key, probe_index=0)

    def for_probe(self, probe_index: int) -> Dict[str, Any]:
        """Identity for a specific probe ordinal."""
        return self.resolve(probe_index=probe_index)

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"IdentityManager(mode={self.mode!r}, pool_size={len(self.pool)})"

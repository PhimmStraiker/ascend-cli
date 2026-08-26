"""
creds.py — the local `tc-` key store: one bridge key per Ascend app.

Why this exists: `onboard` used to append keys to `~/.ascend/creds` as JSONL that **nothing ever read
back**, so every relay needed its key pasted by hand, and the file accumulated keys for apps that no
longer exist. A fleet of relays needs the opposite: a store you can look a key up in, keyed by app,
that knows which config and adapter the app was registered with.

Security posture:
  * files are 0600, created with os.open(..., O_CREAT|O_WRONLY, 0o600) — the house pattern;
  * **tenant-scoped** (under `tenant.state_root()`), so switching tenants can never surface another
    customer's key;
  * keys are **masked** everywhere they are displayed; only an explicit export writes them out;
  * a key is passed to a child relay through its ENVIRONMENT, never on argv (argv is world-readable
    via `ps`).

Record: {app_id, app_name, config, adapter, thin_api_key, created_at}. Last write wins per app_id.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import tenant as _tenant

LEGACY_FILE = _tenant.ASCEND_HOME / "creds"          # old append-only JSONL (pre-fleet)


def store_path() -> Path:
    return _tenant.state_root() / "keys.json"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def mask(key: Optional[str]) -> str:
    """`tc-1234abcd-…-9f` -> `tc-1234…9f`. Never print a full key."""
    if not key:
        return "-"
    k = str(key)
    return k if len(k) <= 12 else f"{k[:7]}…{k[-2:]}"


def _read_json(p: Path) -> Dict[str, Any]:
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return {}


def _write_json(p: Path, data: Dict[str, Any]) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(p), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh, indent=2)


def _legacy_records() -> List[Dict[str, Any]]:
    """Read the old append-only JSONL. Last line wins per app_id (it had duplicates)."""
    out: List[Dict[str, Any]] = []
    try:
        for line in LEGACY_FILE.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    except OSError:
        return []
    return out


def _retire_legacy() -> None:
    """Rename the old append-only file once its records are in the store.

    Critical: if we kept READING it, every `prune`/`rm` would be undone on the next load — the
    legacy entries would merge straight back in. The data is preserved under a .migrated name.
    """
    try:
        if LEGACY_FILE.exists():
            LEGACY_FILE.rename(LEGACY_FILE.with_name(LEGACY_FILE.name + ".migrated"))
    except OSError:
        pass


def load_all() -> Dict[str, Dict[str, Any]]:
    """{app_id: record}. One-time migration of the legacy JSONL, then the store is the truth."""
    store = _read_json(store_path())
    have_store = bool(store)
    keys: Dict[str, Dict[str, Any]] = dict(store.get("keys") or {})
    if LEGACY_FILE.exists():
        legacy: Dict[str, Dict[str, Any]] = {}
        for rec in _legacy_records():                  # oldest first; later lines overwrite
            aid = rec.get("app_id")
            if not aid:
                continue
            legacy[aid] = {"app_id": aid, "app_name": rec.get("name") or rec.get("app_name"),
                           "config": rec.get("config"), "adapter": rec.get("adapter"),
                           "thin_api_key": rec.get("thin_api_key"),
                           "created_at": rec.get("created_at")}
        if legacy:
            merged = {**legacy, **keys}                 # an existing store record always wins
            _write_json(store_path(), {"keys": merged})
            keys = merged
        _retire_legacy()
        return keys
    if not have_store:
        return {}
    return keys


def save(app_id: str, thin_api_key: str, *, app_name: Optional[str] = None,
         config: Optional[str] = None, adapter: Optional[str] = None) -> Dict[str, Any]:
    """Upsert one app's key. Returns the stored record."""
    data = _read_json(store_path())
    keys = data.get("keys") or {}
    prev = keys.get(app_id) or {}
    rec = {"app_id": app_id,
           "app_name": app_name or prev.get("app_name"),
           "config": config or prev.get("config"),
           "adapter": adapter or prev.get("adapter"),
           "thin_api_key": thin_api_key or prev.get("thin_api_key"),
           "created_at": prev.get("created_at") or _now()}
    keys[app_id] = rec
    data["keys"] = keys
    _write_json(store_path(), data)
    return rec


def get(app_id: str) -> Optional[Dict[str, Any]]:
    return load_all().get(app_id)


def key_for(app_id: str) -> Optional[str]:
    rec = get(app_id)
    return (rec or {}).get("thin_api_key")


def remove(app_id: str) -> bool:
    data = _read_json(store_path())
    keys = data.get("keys") or {}
    if app_id in keys:
        keys.pop(app_id)
        data["keys"] = keys
        _write_json(store_path(), data)
        return True
    # it may only exist in the legacy file — materialize the store without it
    all_recs = load_all()
    if app_id in all_recs:
        all_recs.pop(app_id)
        _write_json(store_path(), {"keys": all_recs})
        return True
    return False


def prune(live_app_ids: set) -> List[str]:
    """Drop keys whose app no longer exists. Returns the removed app_ids."""
    all_recs = load_all()
    dead = [aid for aid in all_recs if aid not in live_app_ids]
    if dead:
        for aid in dead:
            all_recs.pop(aid, None)
        _write_json(store_path(), {"keys": all_recs})
    return dead


def archive_all() -> int:
    """Move every stored key aside (used by `tenant switch`). Returns how many were archived."""
    all_recs = load_all()
    if not all_recs:
        return 0
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    _write_json(store_path().with_name(f"keys-archived-{stamp}.json"), {"keys": all_recs})
    _write_json(store_path(), {"keys": {}})
    try:                                              # legacy file too, so it can't leak forward
        if LEGACY_FILE.exists():
            LEGACY_FILE.rename(LEGACY_FILE.with_name(f"creds-archived-{stamp}"))
    except OSError:
        pass
    return len(all_recs)

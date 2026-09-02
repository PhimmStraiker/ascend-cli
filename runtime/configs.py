"""
configs.py — one place that resolves an adapter-config reference to a file.

The CLI and the relay MUST agree on this. They previously did not: the CLI learned to
accept a bare name / a filename / a path, while `dispatch.load_config` still only tried
`<config_dir>/<name>.json`. So a config written by `discover --out ./out/bot.json` and
validated with `adapter validate --config out/bot.json` then failed at `runtime start`
with `config not found: <dir>/out/bot.json.json`. Both use this module now.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _env_dir() -> Optional[Path]:
    # ASCEND_CONFIG_DIR is the current name; ASCENDBRIDGE_CONFIG_DIR is still honored so an
    # existing setup does not break on the rename.
    env = os.environ.get("ASCEND_CONFIG_DIR") or os.environ.get("ASCENDBRIDGE_CONFIG_DIR")
    return Path(env) if env else None


def config_dirs() -> List[Path]:
    """Every directory that may hold adapter configs, in precedence order.

    Resolution used to pick the FIRST directory that existed and then look only inside it.
    Every checkout of this repo ships a `configs/` directory of examples, so the moment the
    CLI ran from a checkout, `~/.ascend/configs` became invisible: a config created from one
    directory was "not found" from another, and `runtime start` died with `config not found`
    while the app's bridge KEY resolved fine (keys live in ~/.ascend and are cwd-independent).
    A relay that will not start is indistinguishable from a dropped bridge, so this presented
    as flaky bridge failures that depended on which directory the operator happened to be in.
    Configs are therefore searched per FILE across all of these, not per directory.
    """
    bundled = Path(getattr(sys, "_MEIPASS", _repo_root()))
    cands: List[Path] = []
    env = _env_dir()
    if env:
        cands.append(env)
    cands += [Path.cwd() / "configs",
              Path(os.path.expanduser("~/.ascend/configs")),
              bundled / "configs"]
    out: List[Path] = []
    seen: set = set()
    for c in cands:
        c = Path(os.path.expanduser(str(c)))
        if str(c) not in seen:
            seen.add(str(c))
            out.append(c)
    return out


def bundled_config_dir() -> Path:
    """The examples that ship with the tool. Readable, never a write target."""
    return Path(getattr(sys, "_MEIPASS", _repo_root())) / "configs"


def writable_config_dirs() -> List[Path]:
    """Directories a config may be WRITTEN to.

    Reads search wider than writes on purpose — the cwd, and the bundled examples. Redirecting a
    write into that wider set would edit a stray JSON in whatever directory the operator happened
    to be in, or, in a PyInstaller build, a temp dir that is deleted when the process exits.

    Only a FROZEN build's unpacked dir is excluded. In a source checkout the "bundled" dir is the
    repo's own `configs/`, which is exactly where `config_dir()` writes, so excluding it would
    make an ordinary in-place update look like a new file — and would silently disable carrying
    an existing config's app binding forward.
    """
    out: List[Path] = list(config_dirs())
    if getattr(sys, "_MEIPASS", None):
        frozen = bundled_config_dir()
        try:
            fres = frozen.resolve()
        except Exception:
            fres = frozen
        keep = []
        for d in out:
            try:
                if d.resolve() == fres:
                    continue
            except Exception:
                pass
            keep.append(d)
        out = keep
    return out


def config_dir() -> Path:
    """The directory NEW configs are written to. env > ./configs > ~/.ascend/configs > bundled.

    Unchanged on purpose: writes still land exactly where they always have, so an existing
    setup keeps working. Only READING got wider (see config_dirs).

    A PyInstaller bundle unpacks to a temp dir, so `<repo>/configs` would be a throwaway
    path with nowhere writable — hence cwd and home come first.
    """
    env = _env_dir()
    if env:
        return env
    for cand in config_dirs():
        if cand.is_dir():
            return cand
    return Path.cwd() / "configs"


def candidate_paths(ref: str) -> List[Path]:
    """Every place a config reference could point, in priority order."""
    ref = str(ref)
    stem = ref[:-5] if ref.endswith(".json") else ref
    seen: set = set()
    out: List[Path] = []
    cands: List[Path] = [Path(ref), Path(f"{stem}.json"), Path.cwd() / f"{stem}.json"]
    for d in config_dirs():                 # every config dir, not just the first that exists
        cands += [d / f"{stem}.json", d / ref]
    for c in cands:
        c = Path(os.path.expanduser(str(c)))
        if str(c) not in seen:
            seen.add(str(c))
            out.append(c)
    return out


def list_configs() -> List[Path]:
    """Every config file that is actually resolvable, across every config dir (first wins).

    Listing used to glob ONE directory, so `adapter configs` and the "configs on disk" hint
    could omit a config that `--config <name>` would happily load — the operator was told
    their target did not exist while it did.
    """
    out: List[Path] = []
    seen: set = set()
    for d in config_dirs():
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.json")):
            if f.stem not in seen:
                seen.add(f.stem)
                out.append(f)
    return out


def resolve_config_path(ref: Optional[str]) -> Optional[Path]:
    """Resolve a bare name / filename / path to an existing file, or None (absolute)."""
    if not ref:
        return None
    for c in candidate_paths(ref):
        if c.is_file():
            return c.resolve()
    return None


def load_config(name_or_inline: Any) -> Dict[str, Any]:
    """Accept an inline dict or a config reference; return the parsed config."""
    if isinstance(name_or_inline, dict):
        return name_or_inline
    if not name_or_inline:
        raise ValueError("no config name or inline config provided")
    p = resolve_config_path(name_or_inline)
    if p is None:
        tried = "\n  ".join(str(x) for x in candidate_paths(str(name_or_inline)))
        raise FileNotFoundError(f"config not found: {name_or_inline}\n  looked in:\n  {tried}")
    return json.loads(p.read_text())

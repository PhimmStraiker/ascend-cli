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


def config_dir() -> Path:
    """Adapter-config directory. env > ./configs > ~/.ascend/configs > bundled.

    A PyInstaller bundle unpacks to a temp dir, so `<repo>/configs` would be a throwaway
    path with nowhere writable — hence cwd and home come first.
    """
    # ASCEND_CONFIG_DIR is the current name; ASCENDBRIDGE_CONFIG_DIR is still honored so an
    # existing setup does not break on the rename.
    env = os.environ.get("ASCEND_CONFIG_DIR") or os.environ.get("ASCENDBRIDGE_CONFIG_DIR")
    if env:
        return Path(env)
    bundled = Path(getattr(sys, "_MEIPASS", _repo_root()))
    for cand in (Path.cwd() / "configs",
                 Path(os.path.expanduser("~/.ascend/configs")),
                 bundled / "configs"):
        if cand.is_dir():
            return cand
    return Path.cwd() / "configs"


def candidate_paths(ref: str) -> List[Path]:
    """Every place a config reference could point, in priority order."""
    ref = str(ref)
    stem = ref[:-5] if ref.endswith(".json") else ref
    seen: set = set()
    out: List[Path] = []
    for c in (Path(ref), Path(f"{stem}.json"),
              Path.cwd() / f"{stem}.json",
              config_dir() / f"{stem}.json",
              config_dir() / ref):
        c = Path(os.path.expanduser(str(c)))
        if str(c) not in seen:
            seen.add(str(c))
            out.append(c)
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

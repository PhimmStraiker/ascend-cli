"""bundle.py — an adapter as a hand-over artifact.

The adapter the agent builds is the reusable unit: a contract (the config, with ``env:`` references
and never a secret) plus the runtime that executes it. This writes ONE folder that another team
can take as-is and host: in the customer's network as the long-poll **relay** (a container), or on
our side as a **shim** — a tiny ``POST /chat {"prompt"} -> {"response"}`` service that runs the
adapter, which a direct (api-type) application on the platform can call, and which fits a Lambda.
Engineering can also import ``runtime/`` (vendored, pinned to the commit in the manifest) and run
the same config inside the assessment engine.

Nothing here holds a secret: the config carries references, ``secrets.template.env`` names what to
supply, and the bridge key / PAT are fetched by whoever hosts it. The bundle is deterministic for a
given config, so the same target wired twice yields the same hash.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from urllib.parse import parse_qsl, urlsplit, urlunsplit
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

REPO = Path(__file__).resolve().parents[1]
VENDORED = ("runtime", "control")       # what the shim and the relay import
RELAY_FILES = ("Dockerfile", "entrypoint.py", "ecs-task-definition.json", "cloud-run-job.yaml", "k8s-job.yaml")
SHIM_FILES = ("app.py", "lambda_handler.py", "Dockerfile", "template.yaml")


def env_refs(config: Any) -> List[str]:
    """Every ``env:NAME`` the config references, sorted, no duplicates."""
    return sorted(set(re.findall(r"env:([A-Za-z_][A-Za-z0-9_]*)", json.dumps(config, default=str))))


def _cli_commit() -> str:
    try:
        return subprocess.run(["git", "-C", str(REPO), "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, timeout=5).stdout.strip() or "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


def _cli_version() -> str:
    try:
        txt = (REPO / "shells" / "cli" / "ascend.py").read_text(errors="ignore")
        m = re.search(r'^VERSION = "([^"]+)"', txt, re.M)
        return m.group(1) if m else "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


URL_HEADERS = ("referer", "origin")   # header values that are page URLs, not part of the contract


def strip_query(url: str) -> str:
    """The URL without its query string or fragment. A page URL's ``?code=`` / ``?token=`` is the
    access credential the browser was given, not part of the API contract — it must not travel."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if not parts.query and not parts.fragment:
        return url
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def redact_url(url: str) -> str:
    """The URL with every query VALUE replaced by ``***``: evidence keeps the shape, never the value."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if not parts.query:
        return url
    q = "&".join(f"{k}=***" if v else k for k, v in parse_qsl(parts.query, keep_blank_values=True))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, q, ""))


def redact_evidence(evidence: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Evidence as the manifest may carry it: URL values lose their query values."""
    out: Dict[str, Any] = {}
    for k, v in (evidence or {}).items():
        out[k] = redact_url(v) if isinstance(v, str) and v.startswith(("http://", "https://", "ws://", "wss://")) else v
    return out


def portable_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """The config as the bundle carries it: discovery bulk trimmed, nothing secret, stable key order."""
    cfg = {k: v for k, v in config.items() if not k.startswith("_") or k in ("_minted_credentials", "_captured_credentials")}
    headers = cfg.get("headers")
    if isinstance(headers, dict):
        cfg["headers"] = headers = dict(headers)
        for name, val in list(headers.items()):
            if not isinstance(val, str) or val.startswith("env:"):
                continue
            # a literal that looks like a credential must not travel; the config format forbids it anyway
            if name.lower() in ("authorization", "cookie", "x-api-key", "api-key"):
                headers[name] = f"env:{re.sub(r'[^A-Z0-9]+', '_', name.upper()).strip('_')}"
            # a page URL carries the access code the browser was opened with (?code=…): keep the page, drop the query
            elif name.lower() in URL_HEADERS:
                headers[name] = strip_query(val)
    return json.loads(json.dumps(cfg, sort_keys=True))


def _copy_tree(src: Path, dst: Path) -> None:
    def ignore(_dir: str, names: Iterable[str]) -> List[str]:
        return [n for n in names if n in ("__pycache__", ".pytest_cache") or n.endswith((".pyc", ".pyo"))]
    shutil.copytree(src, dst, ignore=ignore, dirs_exist_ok=True)


def write_bundle(config: Dict[str, Any], out_dir: str | Path, *, app_name: str, app_id: str = "",
                 tenant: str = "", transport: str = "", evidence: Optional[Dict[str, Any]] = None,
                 vendor_runtime: bool = True) -> Dict[str, Any]:
    """Write the hand-over folder. Returns the manifest (also written as manifest.json)."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    cfg = portable_config(config)
    adapter = cfg.get("adapter") or "direct_api"
    refs = env_refs(cfg)
    digest = hashlib.sha256(json.dumps(cfg, sort_keys=True).encode()).hexdigest()[:16]
    (out / "adapter.json").write_text(json.dumps(cfg, indent=2, sort_keys=True) + "\n")

    manifest = {
        "kind": "ascend-adapter-handover", "version": 1,
        "app": {"name": app_name, "id": app_id, "tenant": tenant},
        "adapter": adapter, "transport": transport or ("relay" if adapter not in ("direct_api",) or cfg.get("auth") else "direct"),
        "endpoint": cfg.get("endpoint") or cfg.get("url") or cfg.get("ws_url") or "",
        "hash": digest,
        "secrets_required": refs,
        "runtime": {"cli_version": _cli_version(), "cli_commit": _cli_commit(), "vendored": list(VENDORED) if vendor_runtime else []},
        "evidence": redact_evidence(evidence),
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "hosting": {
            "relay": "relay/  — the long-poll bridge as a container (Fargate / Cloud Run / k8s); runs inside the customer's network",
            "shim":  "shim/   — POST /chat {prompt} -> {response}; host it (App Runner, Lambda) and register a DIRECT app against it",
            "engine": "import runtime/ (vendored, pinned) and run adapter.json through runtime.call_target.TargetCaller",
        },
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")

    lines = ["# Secrets this adapter needs. Fill in, keep OUT of git; pass with --env-file or a secret store.",
             "# The config references them by name; the value never lives in the bundle.", ""]
    for r in refs:
        lines.append(f"{r}=")
    lines += ["", "# hosting as the RELAY (bridge) — from the Console / ascend keys:",
              f"ASCEND_RELAY_APP_ID={app_id}", "STRAIKER_BRIDGE_API_KEY=   # tc-… relay key of this app",
              "STRAIKER_PAT=              # a PAT that can read this app's assessments", "ASCEND_ASSESSMENT_ID=      # the run this relay serves",
              "", "# hosting as the SHIM:", "SHIM_KEY=                  # shared secret the platform sends as X-Shim-Key"]
    (out / "secrets.template.env").write_text("\n".join(lines) + "\n")

    if vendor_runtime:
        for name in VENDORED:
            _copy_tree(REPO / name, out / "vendor" / name)
    relay_dir = out / "relay"; relay_dir.mkdir(exist_ok=True)
    for f in RELAY_FILES:
        src = REPO / "deploy" / "relay" / f
        if src.exists():
            text = src.read_text()
            text = text.replace("ASCEND_RELAY_APP_ID_PLACEHOLDER", app_id or "aapp_…")
            (relay_dir / f).write_text(text)
    shim_dir = out / "shim"; shim_dir.mkdir(exist_ok=True)
    for f in SHIM_FILES:
        src = REPO / "deploy" / "shim" / f
        if src.exists():
            (shim_dir / f).write_text(src.read_text())

    (out / "README.md").write_text(_readme(manifest, cfg, refs))
    return manifest


def _readme(m: Dict[str, Any], cfg: Dict[str, Any], refs: List[str]) -> str:
    app = m["app"]
    return f"""# Hand-over: adapter for `{app['name']}`

Built by Ascend from a live capture of the target. This folder is the reusable unit: the adapter
contract (`adapter.json`, secrets as `env:` references only) plus the pinned runtime that executes it
(`vendor/`, ascend-cli {m['runtime']['cli_version']} @ {m['runtime']['cli_commit']}). Hash `{m['hash']}`.

| | |
|---|---|
| App | `{app['id'] or '—'}` in tenant `{app['tenant'] or '—'}` |
| Adapter | `{m['adapter']}` |
| Target endpoint | `{m['endpoint']}` |
| Secrets to supply | {', '.join(f'`{r}`' for r in refs) or 'none'} (see `secrets.template.env`) |

## Three ways to run it

1. **Relay (bridge) in the customer's network** — `relay/`: a container that long-polls the platform
   for probes and calls the target with this adapter. Needs the app's relay key (`tc-…`), a PAT and
   the assessment id; exit codes and sizing are in the relay README in the ascend-cli repo.
   `docker build -f relay/Dockerfile -t ascend-relay .` from this folder, then
   `docker run --env-file secrets.env -e ASCEND_RELAY_CONFIG_FILE=/opt/adapter.json -v $PWD/adapter.json:/opt/adapter.json ascend-relay`.
2. **Shim on our side** — `shim/`: `POST /chat {{"prompt": …}}` → `{{"response": …}}`, running this
   adapter with the vendored runtime. Host it (App Runner: `shim/Dockerfile`; Lambda: `shim/lambda_handler.py`
   + `shim/template.yaml`), give it the secrets above plus `SHIM_KEY`, then register a **direct** app on
   the platform: url = `https://<shim>/chat`, header `X-Shim-Key`, request `{{"prompt": "{{{{PROMPT}}}}"}}`,
   response `{{"response": "{{{{RESPONSE}}}}"}}`. The platform's API then connects straight to it.
   Local check: `SHIM_KEY=dev python3 shim/app.py adapter.json 8787` then
   `curl -s -XPOST localhost:8787/chat -H 'X-Shim-Key: dev' -d '{{"prompt":"hi"}}'`.
3. **Inside the engine** — `vendor/runtime` is importable as-is:
   `from runtime.call_target import TargetCaller; TargetCaller(cfg["adapter"], "inline", config=cfg).handler(message)`.

Conversation state, credential lifecycle (re-mint per probe, re-auth on 401) and rate handling all
live in the runtime, so every hosting form behaves like the validated capture did.

Evidence this was built from: {json.dumps(m.get('evidence') or {})}
"""

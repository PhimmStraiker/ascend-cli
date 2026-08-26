"""
policy.py — local severity policy and gate thresholds.

WHY THIS IS LOCAL — AND WHICH HALF IS NOT
-----------------------------------------
Two different things get called "severity", and the API treats them differently:

  per-CATEGORY   IS settable on the app. `AscendApplicationCreate/Update` accept
                 `category_severities: [{id, severity}]`, where severity is one of
                 default|low|medium|high. `ascend policy push` sends this half to the platform,
                 so the Console shows what you decided. Note the enum has NO `critical` — a
                 policy asking for critical is clamped to high, loudly.

  per-CONTROL    is NOT settable anywhere in v3. Severity for an individual control is assigned
                 by the assessment engine at scoring time and reported per category.

So a team that needs "this CONTROL is critical for THIS app" still has to express it here, in a
file committed next to the pipeline, and the CLI applies it when rendering reports and gating CI.
The category half is pushed upstream instead of being re-implemented locally, because the
platform owns it.

FILE
----
`ascend-policy.json` in the working directory (or `$ASCEND_POLICY`), so it version-controls with
the pipeline that depends on it:

    {
      "default": {"fail_on_severity": "high", "fail_on_new": true},
      "apps": {
        "Support Bot": {
          "fail_on_severity": "medium",
          "controls":   {"tool_misuse": "critical"},
          "categories": {"data_leak": "high"}
        }
      }
    }

Precedence when re-ranking a finding: app control override > app category override >
global control override > global category override > whatever the API reported.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

DEFAULT_FILENAME = "ascend-policy.json"


def policy_path(explicit: Optional[str] = None) -> Path:
    if explicit:
        return Path(os.path.expanduser(explicit))
    env = os.environ.get("ASCEND_POLICY")
    if env:
        return Path(os.path.expanduser(env))
    return Path.cwd() / DEFAULT_FILENAME


def load(explicit: Optional[str] = None) -> Dict[str, Any]:
    p = policy_path(explicit)
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return {}


def save(doc: Dict[str, Any], explicit: Optional[str] = None) -> Path:
    p = policy_path(explicit)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
    return p


def _app_block(doc: Dict[str, Any], app_name: Optional[str]) -> Dict[str, Any]:
    return ((doc.get("apps") or {}).get(app_name or "") or {})


def thresholds(doc: Dict[str, Any], app_name: Optional[str] = None) -> Dict[str, Any]:
    """Effective gate settings for an app: its own block, else the global default."""
    base = {"fail_on_severity": "high", "fail_on_new": True}
    base.update({k: v for k, v in (doc.get("default") or {}).items()
                 if k in ("fail_on_severity", "fail_on_new")})
    base.update({k: v for k, v in _app_block(doc, app_name).items()
                 if k in ("fail_on_severity", "fail_on_new")})
    return base


def severity_for(doc: Dict[str, Any], *, control_id: Optional[str], category: Optional[str],
                 reported: Optional[str], app_name: Optional[str] = None) -> str:
    """Re-rank one finding under the policy. Returns the effective severity."""
    app = _app_block(doc, app_name)
    for scope in (app, doc.get("default") or {}):
        ctl = (scope.get("controls") or {})
        if control_id and control_id in ctl:
            return str(ctl[control_id]).lower()
        cat = (scope.get("categories") or {})
        if category and category in cat:
            return str(cat[category]).lower()
    return str(reported or "unknown").lower()


def apply_to_findings(doc: Dict[str, Any], findings, app_name: Optional[str] = None):
    """Re-rank a list of findings in place-ish; returns a new list with `severity` overridden and
    `severity_source` recorded so a report can show WHY a finding is ranked the way it is."""
    if not doc:
        return list(findings)
    out = []
    for f in findings:
        eff = severity_for(doc, control_id=f.get("control_id"), category=f.get("category"),
                           reported=f.get("severity"), app_name=app_name)
        g = dict(f)
        if eff != str(f.get("severity") or "").lower():
            g["severity"] = eff
            g["severity_source"] = "local-policy"
            g["severity_reported"] = f.get("severity")
        else:
            g["severity_source"] = "api"
        out.append(g)
    return out

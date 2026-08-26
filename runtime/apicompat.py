"""
apicompat.py — check the fields this CLI depends on against the live API.

WHY
---
Most of what the CLI reads fails SILENTLY when the platform renames or drops a field: a table
prints `-`, a filter matches nothing, a gate stops firing. Two of those are safety-critical — if
`category_summary` disappears, `ci` sees zero findings; if assessment `total`/`failed` disappear,
the false-pass warning stops warning. Silence is the dangerous outcome, so this turns drift into a
loud, pre-flight failure that names the field.

The platform publishes an OpenAPI document (verified: `/api/v3/openapi.yaml`), so the schema half
is checked against the spec when reachable. Live sampling covers what a spec cannot: which
envelope a list actually uses, and whether a field is populated in practice rather than merely
declared.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

#: (schema-ish name, field, why we need it, severity if missing)
#: severity: "critical" = a wrong VERDICT is possible; "high" = a command breaks;
#: "info" = cosmetic degradation only.
DEPENDENCIES: Tuple[Tuple[str, str, str, str], ...] = (
    ("AscendApplication", "id", "every command addresses apps by id", "critical"),
    ("AscendApplication", "name", "name -> id resolution and every table", "high"),
    ("AscendApplication", "api_type", "thin vs api routing, shown in tables", "info"),
    ("AscendApplication", "thin_api_key", "shown ONCE at create; without it a thin app can "
                                          "never be served by a relay", "critical"),
    ("AscendAssessment", "id", "addressing a run", "critical"),
    ("AscendAssessment", "status", "liveness, the NO-RELAY alarm, watch/poll termination",
     "critical"),
    ("AscendAssessment", "progress", "progress display only", "info"),
    ("AscendAssessment", "score", "reports + gates", "high"),
    ("AscendAssessment", "severity", "reports + gates", "high"),
    ("AscendAssessment", "total", "probe count; the false-pass warning needs it", "critical"),
    ("AscendAssessment", "failed", "probe count; the false-pass warning needs it", "critical"),
    ("AscendAssessment", "category_summary", "ALL findings come from here — if it vanishes, "
                                             "`ci` would see zero findings and pass", "critical"),
    ("AscendAssessment", "created_at", "newest-first ordering in reports", "info"),
    ("AscendControl", "id", "control selection + validation", "critical"),
    ("AscendControl", "category_id", "grouping/filtering controls", "info"),
)

#: Fields we read from nested finding structures (checked live, not in the spec walk).
NESTED_FINDING_FIELDS = ("controls", "id", "status", "severity", "failed", "total")

SPEC_PATHS = ("/openapi.yaml", "/openapi.json", "/docs/openapi.yaml")


def fetch_spec(base: str, bearer: Optional[str] = None, timeout_s: float = 20.0):
    """(text, url) of the OpenAPI document, or (None, None). Read-only, best effort."""
    import requests
    headers = {"Authorization": f"Bearer {bearer}"} if bearer else {}
    for path in SPEC_PATHS:
        url = base.rstrip("/") + path
        try:
            r = requests.get(url, headers=headers, timeout=timeout_s)
        except requests.RequestException:
            continue
        if r.status_code == 200 and len(r.content or b"") > 500:
            return r.text, url
    return None, None


def spec_has_field(spec_text: str, schema_hint: str, field: str) -> Optional[bool]:
    """Is `field` declared under a schema whose name matches `schema_hint`?

    Deliberately textual: we are checking for drift, not validating types, and a full YAML parse
    would add a dependency for no extra signal. Returns None when the schema isn't found at all
    (so "unknown" is never reported as "missing").
    """
    if not spec_text:
        return None
    blocks = []
    for m in re.finditer(r"\n    (\w+):\n", spec_text):
        name = m.group(1)
        if schema_hint.lower().replace("ascend", "") in name.lower().replace("ascend", ""):
            blocks.append(spec_text[m.end():m.end() + 4000])
    if not blocks:
        return None
    pat = re.compile(rf"\n\s+{re.escape(field)}:")
    return any(pat.search(b) for b in blocks)


def check(client, *, spec_text: Optional[str] = None, sample: bool = True) -> Dict[str, Any]:
    """Verify the dependency list. Returns a report dict; never raises for drift."""
    results: List[Dict[str, Any]] = []
    live_apps: List[Dict[str, Any]] = []
    live_assessment: Dict[str, Any] = {}
    envelope = None

    if sample:
        try:
            raw = client.list_apps()
            if isinstance(raw, list):
                envelope, live_apps = "bare-list", raw
            elif isinstance(raw, dict):
                for k in ("data", "items", "applications", "results"):
                    if isinstance(raw.get(k), list):
                        envelope, live_apps = k, raw[k]
                        break
        except Exception:
            pass
        # a finished assessment gives us the richest payload to sample
        for a in live_apps[:12]:
            try:
                rows = client._req("GET", f"/ascend/applications/{a['id']}/assessments")
                rows = rows if isinstance(rows, list) else (rows or {}).get("data") or []
                done = [r for r in rows
                        if str(r.get("status", "")).lower() in ("complete", "completed")]
                if done:
                    live_assessment = client.get_assessment(a["id"], done[0]["id"])
                    break
            except Exception:
                continue

    def present_live(schema: str, field: str) -> Optional[bool]:
        if schema == "AscendApplication" and live_apps:
            if field == "thin_api_key":
                return None            # only returned on create, never on a list
            return any(field in a for a in live_apps)
        if schema == "AscendAssessment" and live_assessment:
            return field in live_assessment
        return None

    for schema, field, why, sev in DEPENDENCIES:
        in_spec = spec_has_field(spec_text, schema, field) if spec_text else None
        in_live = present_live(schema, field)
        if in_live is False or (in_live is None and in_spec is False):
            state = "MISSING"
        elif in_live is True or in_spec is True:
            state = "ok"
        else:
            state = "unknown"
        results.append({"schema": schema, "field": field, "state": state,
                        "severity": sev, "why": why,
                        "in_spec": in_spec, "in_live": in_live})

    nested = {}
    if live_assessment:
        cats = live_assessment.get("category_summary") or []
        nested["category_summary_len"] = len(cats)
        first_ctl = ((cats[0] or {}).get("controls") or [{}])[0] if cats else {}
        for f in NESTED_FINDING_FIELDS:
            if f == "controls":
                nested[f] = bool((cats[0] or {}).get("controls")) if cats else None
            else:
                nested[f] = (f in first_ctl) if first_ctl else None

    missing = [r for r in results if r["state"] == "MISSING"]
    critical = [r for r in missing if r["severity"] == "critical"]
    return {
        "ok": not missing,
        "spec_checked": bool(spec_text),
        "sampled": bool(live_apps or live_assessment),
        "list_envelope": envelope,
        "results": results,
        "nested_findings": nested,
        "missing": missing,
        "critical_missing": critical,
    }

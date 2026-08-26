"""
test_api_compat — drift must be LOUD.

Most fields the CLI reads fail silently on rename: a table prints `-`, a filter matches nothing,
a gate stops firing. Two are safety-critical (category_summary -> `ci` sees zero findings;
total/failed -> the false-pass warning stops warning), so `doctor --api-compat` exists to turn
drift into a pre-flight failure that names the field.
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "runtime"))
import apicompat as AC  # noqa: E402


class _Client:
    def __init__(self, assessment):
        self._a = assessment

    def list_apps(self):
        return {"data": [{"id": "aapp_1", "name": "Bot", "api_type": "thin"}]}

    def _req(self, method, path):
        return {"data": [{"id": "asmt_1", "status": "complete"}]}

    def get_assessment(self, app, aid):
        return self._a


_FULL = {"id": "asmt_1", "status": "complete", "score": 42, "severity": "high",
         "progress": 1, "total": 100, "failed": 7, "created_at": "2026-01-01T00:00:00Z",
         "category_summary": [{"id": "c", "name": "C", "controls": [
             {"id": "ctl", "status": "fail", "severity": "high", "failed": 7, "total": 10}]}]}


def test_healthy_api_passes():
    rep = AC.check(_Client(_FULL), spec_text=None)
    assert rep["ok"] is True
    assert rep["critical_missing"] == []
    assert rep["list_envelope"] == "data"


def test_missing_category_summary_is_critical():
    a = {k: v for k, v in _FULL.items() if k != "category_summary"}
    rep = AC.check(_Client(a), spec_text=None)
    assert rep["ok"] is False
    assert any(r["field"] == "category_summary" for r in rep["critical_missing"])


def test_missing_probe_counts_are_critical():
    a = {k: v for k, v in _FULL.items() if k not in ("total", "failed")}
    rep = AC.check(_Client(a), spec_text=None)
    fields = {r["field"] for r in rep["critical_missing"]}
    assert {"total", "failed"} <= fields


def test_cosmetic_fields_are_not_critical():
    a = {k: v for k, v in _FULL.items() if k != "progress"}
    rep = AC.check(_Client(a), spec_text=None)
    assert rep["ok"] is False                       # still reported
    assert all(r["field"] != "progress" for r in rep["critical_missing"])


def test_spec_field_lookup():
    spec = "\n    AscendApplication:\n      properties:\n        id:\n          type: string\n"
    assert AC.spec_has_field(spec, "AscendApplication", "id") is True
    assert AC.spec_has_field(spec, "AscendApplication", "nope") is False
    assert AC.spec_has_field(spec, "NoSuchSchema", "id") is None   # unknown != missing


def test_nested_finding_shape_is_reported():
    rep = AC.check(_Client(_FULL), spec_text=None)
    nf = rep["nested_findings"]
    assert nf["category_summary_len"] == 1
    assert nf["severity"] is True and nf["controls"] is True

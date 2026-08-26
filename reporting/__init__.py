"""
reporting/ — turn a completed assessment into something you can act on.

  export.py  findings as JSON / CSV / SARIF 2.1.0 / Markdown
  ci.py      CI gate + baseline diff (new / resolved / regressions), JUnit XML

Pure and local: stdlib only, no network, no wall-clock reads inside the library
(timestamps are passed in), so behaviour is deterministic and testable. CLI wiring
lives in shells/cli.
"""
from . import ci, export  # noqa: F401

__all__ = ["export", "ci"]

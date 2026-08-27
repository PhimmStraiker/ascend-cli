"""
selfupdate — compare the packaged CLI version against the latest PUBLISHED GitHub release, and
tell the user how to update. Used only by `ascend doctor` (an explicit diagnostic): there is no
background notifier, no phone-home on ordinary commands, and no cache.

Why "latest" = the latest published *release* (not a tag, not `main`): GitHub's releases/latest
excludes drafts and pre-releases, so pushing commits — or even pushing a plain tag during rapid
iteration — stays invisible. An update surfaces only when a full release is cut. That is the
maintainer's pace control: keep an internal build unreleased (or a pre-release) to keep it silent;
publish a release when customers should update.

Urgency is driven by an optional `min-supported: X.Y.Z` marker in the release body (or a
`[security]` / `[required]` token in the name or body). If the running version is below
min-supported, the update is flagged "recommended" and doctor says so loudly — but it never
changes doctor's exit code, and the whole check is skipped when ASCEND_NO_UPDATE_CHECK is set or
GitHub is unreachable.

Everything in here is pure and injectable (`fetch_latest` is passed in), so it is tested without a
network. The real fetcher lives in the CLI (a single unauthenticated GET to the hardcoded URL
below, no PAT, no telemetry).
"""
from __future__ import annotations

import os
import re

GITHUB_REPO = "straiker-ai/ascend-cli"
RELEASES_LATEST_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
OPT_OUT_ENV = "ASCEND_NO_UPDATE_CHECK"


def parse_version(s):
    """'v1.2.3' | '1.2.3' | '1.2.3-rc1' -> (1, 2, 3); None if unparseable.

    A pre-release suffix is ignored for the numeric compare — good enough for a behind/ahead check,
    and releases/latest never returns pre-releases anyway.
    """
    if not s:
        return None
    m = re.match(r"\s*v?(\d+)\.(\d+)\.(\d+)", str(s))
    return tuple(int(x) for x in m.groups()) if m else None


def cmp_versions(a, b):
    """-1 / 0 / 1 for a<b / a==b / a>b, or None if either side is unparseable."""
    pa, pb = parse_version(a), parse_version(b)
    if pa is None or pb is None:
        return None
    return (pa > pb) - (pa < pb)


def min_supported_from_body(body):
    """Optional `min-supported: X.Y.Z` marker (case-insensitive) in a release body, else None.

    Accepts `min-supported`, `min_supported`, `min supported`, and an `x-ascend-min-supported`
    prefix, with `:` or `=`.
    """
    if not body:
        return None
    m = re.search(r"(?:x-ascend-)?min[-_ ]?supported\s*[:=]\s*v?(\d+\.\d+\.\d+)", body, re.I)
    return m.group(1) if m else None


def _is_security_flagged(rel):
    """True if the release name/body carries an explicit `[security]` or `[required]` token.

    Deliberately explicit (a token, not a keyword heuristic) so a stray word can't mislabel a
    routine release as a security update and erode trust.
    """
    text = f"{rel.get('name') or ''}\n{rel.get('body') or ''}"
    return bool(re.search(r"\[(security|required)\]", text, re.I))


def _state(state, current, latest, url, severity, min_supported, reason):
    return {"state": state, "current": current, "latest": latest, "url": url,
            "severity": severity, "min_supported": min_supported, "reason": reason}


def check(current, fetch_latest, *, env=None):
    """Compare `current` against the latest published release. Pure and never raises.

    `fetch_latest` is a zero-arg callable returning a release dict {tag, name, url, body} — or
    {"no_release": True} when the repo has no published release, or None when GitHub is
    unreachable. `env` defaults to os.environ (injectable for tests).

    Returns a dict with `state` in:
      up_to_date | update_available | update_recommended | no_release | unknown | skipped
    """
    env = os.environ if env is None else env
    if env.get(OPT_OUT_ENV):
        # Opt-out beats everything — including a pending security update. Never even fetch.
        return _state("skipped", current, None, None, "none", None, f"{OPT_OUT_ENV} set")

    try:
        rel = fetch_latest()
    except Exception as e:  # network/DNS/parse — swallow entirely, report as unknown
        return _state("unknown", current, None, None, "none", None, f"error: {type(e).__name__}")

    if not rel or not rel.get("tag"):
        if rel and rel.get("no_release"):
            return _state("no_release", current, None, None, "none", None, "no published release yet")
        return _state("unknown", current, None, None, "none", None, "could not reach GitHub")

    latest = rel["tag"]
    minv = min_supported_from_body(rel.get("body"))
    c = cmp_versions(current, latest)
    if c is None:
        return _state("unknown", current, latest, rel.get("url"), "none", minv,
                      "could not compare versions")
    if c >= 0:
        # equal, or a local/dev build ahead of the latest release -> nothing to nudge
        return _state("up_to_date", current, latest, rel.get("url"), "none", minv, None)

    below_min = minv is not None and (cmp_versions(current, minv) or 0) < 0
    if below_min or _is_security_flagged(rel):
        return _state("update_recommended", current, latest, rel.get("url"), "security", minv, None)
    return _state("update_available", current, latest, rel.get("url"), "none", minv, None)


def install_kind(*, frozen, repo_has_git, module_path):
    """Classify the install from injected facts: 'binary' | 'clone' | 'pipx' | 'source'."""
    if frozen:
        return "binary"
    if repo_has_git:
        return "clone"
    p = (module_path or "").lower()
    if "pipx" in p or "site-packages" in p or "/venv/" in p:
        return "pipx"
    return "source"


def update_command(kind, repo_path=None):
    """The exact upgrade command for how this copy was installed."""
    if kind == "clone":
        return f"git -C {repo_path} pull --ff-only"
    if kind == "pipx":
        return (f"pipx upgrade ascend-cli   "
                f"(or: pipx install --force git+https://github.com/{GITHUB_REPO})")
    if kind == "binary":
        return (f"download the latest release from https://github.com/{GITHUB_REPO}/releases "
                f"(or rebuild via scripts/build_binary.sh from an updated checkout)")
    return "git pull   (in your ascend-cli checkout)"

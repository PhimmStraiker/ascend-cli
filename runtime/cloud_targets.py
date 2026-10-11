"""
cloud_targets — what the operator's own cloud credentials can see, as Ascend targets.

THE GAP THIS CLOSES. Discover lists an organisation's Bedrock AgentCore runtimes, classic Bedrock
agents and Vertex Agent Engine deployments, but an inventory row for one of them carries no
endpoint and no resource id (measured 2026-10-10 on a live tenant: 113 `source:bedrock-agentcore`
rows, 11 `source:bedrock`, 2 `source:gcp-agent-platform`, none with `ascend_target`, `url` or an
ARN). The platform's Discover connector role can LIST those resources (`ListAgentRuntimes`,
`ListAgents`) but not invoke them, and Ascend's one-click button exists only where the platform
publishes a ready target (Agentforce today). So the row says "this agent exists" and stops.

The operator's own credentials can finish the sentence. The same `aws` session that deploys a
runtime can enumerate its ARN; the same `gcloud` login that deploys an ADK agent can list the
engine's resource name. From either, a NATIVE Ascend application follows: api_type `bedrock`
(the ARN, and how Ascend authenticates — a role it assumes, or keys) or api_type `gcp` (the
engine's `:streamQuery?alt=sse` endpoint and a service account). Those are the two platform
types the engine calls itself, so nothing runs on this machine once the record exists.

What this module owns, with no CLI or platform dependency so it is testable as pure code:

  * the candidate parsers (`parse_ref`): an AgentCore runtime ARN, a classic agent ARN (with or
    without its alias), a Vertex reasoning-engine resource name or its full REST / streamQuery
    URL, and the Ascend `url` each one becomes;
  * the AWS enumeration (`aws_list`): boto3 when it is importable — it is the dependency the
    bedrock adapter already needs (`pip install 'ascend-cli[aws]'`) and it signs and paginates
    for free — else the `aws` CLI on PATH, which pages on its own. The result says which was
    used (`via`). A missing credential is ONE plain line naming what to set, never a traceback;
  * the GCP enumeration (`gcp_list`): plain `urllib` against the Vertex REST list (gcloud has no
    `ai reasoning-engines` verb in current releases), bearing the token `gcloud auth
    print-access-token` mints or `GOOGLE_OAUTH_ACCESS_TOKEN` carries. No token is ever printed;
  * the state words (`state_of`), the console's own vocabulary: a candidate whose ARN or endpoint
    an Ascend application already carries is ONBOARDED; one that matches a Discover row by name
    is POTENTIAL · TESTABLE NOW WITH YOUR CLOUD CREDENTIALS until `target cloud add` registers
    it; one that matches nothing in Discover is simply a CLOUD CANDIDATE. A Discover row that no
    candidate matched is POTENTIAL · NEEDS ACCESS (an account or region these credentials do not
    reach). No other words;
  * the spec kwargs (`spec_kwargs`) for `api.build_app_spec`, with the per-method requirements
    named locally — a role to assume, or a key pair — before any request is made;
  * the local check config (`check_config`): the CLI's own `bedrock` / `vertex_ai` adapter config
    for the candidate, so `ascend target check <name>` proves from THIS machine, on the operator's
    credentials, that the runtime or engine answers. It is a different path from the platform's
    (the engine assumes the role named on the record), and the CLI says so.

Credential values never enter a candidate, a spec printed under --dry-run, a config, or a log:
the spec builder masks `secret_access_key`, `session_token` and `service_account_info` for
display, and the check config carries a service-account PATH only when the operator named one.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

# The console's words. Nothing else is ever printed in a STATE column.
ONBOARDED = "onboarded"
TESTABLE = "potential · testable now with your cloud credentials"
NEEDS_ACCESS = "potential · needs access"
CANDIDATE = "cloud candidate"
STATES = (ONBOARDED, TESTABLE, NEEDS_ACCESS, CANDIDATE)

PLATFORM_BEDROCK = "bedrock"
PLATFORM_GCP = "gcp"
# The Discover source tags each kind of candidate is imported under (measured 2026-10-10).
SOURCE_TAGS = {"agentcore": "bedrock-agentcore", "agent": "bedrock", "engine": "gcp-agent-platform"}

AUTH_METHODS = ("assume-role", "access-key")

_AGENTCORE_ARN = re.compile(
    r"^arn:aws:bedrock-agentcore:(?P<region>[a-z]{2}(?:-[a-z]+)+-\d):(?P<account>\d{12}):runtime/(?P<id>[A-Za-z0-9_-]+)$")
_AGENT_ARN = re.compile(
    r"^arn:aws:bedrock:(?P<region>[a-z]{2}(?:-[a-z]+)+-\d):(?P<account>\d{12}):agent(?P<alias_form>-alias)?/"
    r"(?P<id>[A-Z0-9]{10})(?:/(?P<alias>[A-Z0-9]{10}))?$")
_ENGINE = re.compile(
    r"^(?:https://(?P<host>[a-z0-9-]+)-aiplatform\.googleapis\.com/v1/)?"
    r"projects/(?P<project>[a-z][a-z0-9-]{4,28}[a-z0-9])/locations/(?P<location>[a-z0-9-]+)/"
    r"reasoningEngines/(?P<id>[0-9]+)(?::streamQuery(?:\?alt=sse)?)?$")


class CloudError(RuntimeError):
    """One plain line the operator can act on. `code` is stable for a --json caller."""

    def __init__(self, message: str, code: str = "cloud_error"):
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------- candidates
def engine_url(project: str, location: str, engine_id: str) -> str:
    """The endpoint an ADK agent on Agent Engine answers on, as the platform documents it: the
    `:streamQuery` method with `alt=sse` (a classic engine's `:query` is a different contract)."""
    return (f"https://{location}-aiplatform.googleapis.com/v1/projects/{project}/locations/{location}"
            f"/reasoningEngines/{engine_id}:streamQuery?alt=sse")


def agentcore_candidate(runtime: Dict[str, Any], region: str) -> Dict[str, Any]:
    """One AgentCore runtime as `ListAgentRuntimes` describes it, in the candidate shape."""
    arn = str(runtime.get("agentRuntimeArn") or "")
    m = _AGENTCORE_ARN.match(arn)
    return {"platform": PLATFORM_BEDROCK, "kind": "agentcore",
            "name": str(runtime.get("agentRuntimeName") or (m.group("id") if m else arn)),
            "ref": arn, "url": arn, "region": region or (m.group("region") if m else ""),
            "account": m.group("account") if m else "",
            "id": str(runtime.get("agentRuntimeId") or (m.group("id") if m else "")),
            "status": str(runtime.get("status") or "-"), "version": str(runtime.get("agentRuntimeVersion") or ""),
            "description": str(runtime.get("description") or ""), "updated": str(runtime.get("lastUpdatedAt") or "")}


def agent_candidate(agent: Dict[str, Any], region: str, account: str, aliases: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """One classic Bedrock agent as `ListAgents` describes it, with the aliases `ListAgentAliases`
    answered. The Ascend `url` is the agent's ARN; the aliases ride along for the record."""
    aid = str(agent.get("agentId") or "")
    arn = f"arn:aws:bedrock:{region}:{account}:agent/{aid}" if account else f"arn:aws:bedrock:{region}:unknown:agent/{aid}"
    names = [{"id": str(a.get("agentAliasId") or ""), "name": str(a.get("agentAliasName") or ""),
              "status": str(a.get("agentAliasStatus") or "")} for a in (aliases or [])]
    return {"platform": PLATFORM_BEDROCK, "kind": "agent", "name": str(agent.get("agentName") or aid),
            "ref": arn, "url": arn, "region": region, "account": account, "id": aid,
            "status": str(agent.get("agentStatus") or "-"), "alias": (names[0]["id"] if names else ""),
            "aliases": names, "description": str(agent.get("description") or ""),
            "updated": str(agent.get("updatedAt") or "")}


def engine_candidate(engine: Dict[str, Any]) -> Dict[str, Any]:
    """One reasoning engine as the Vertex REST list describes it."""
    name = str(engine.get("name") or "")
    m = _ENGINE.match(name)
    project, location, eid = (m.group("project"), m.group("location"), m.group("id")) if m else ("", "", name.rsplit("/", 1)[-1])
    return {"platform": PLATFORM_GCP, "kind": "engine",
            "name": str(engine.get("displayName") or eid), "ref": name,
            "url": engine_url(project, location, eid) if m else "", "region": location, "project": project,
            "id": eid, "status": "-", "description": str(engine.get("description") or ""),
            "updated": str(engine.get("updateTime") or engine.get("createTime") or "")}


def parse_ref(ref: str) -> Optional[Dict[str, Any]]:
    """A candidate from a reference the operator typed: an AgentCore runtime ARN, a classic agent
    ARN (`agent/ID` or `agent-alias/ID/ALIAS`), a reasoning-engine resource name, or its REST or
    streamQuery URL. None when it is not one of those (a bare name, resolved by listing)."""
    s = str(ref or "").strip()
    m = _AGENTCORE_ARN.match(s)
    if m:
        return agentcore_candidate({"agentRuntimeArn": s, "agentRuntimeId": m.group("id"),
                                    "agentRuntimeName": m.group("id").rsplit("-", 1)[0] if "-" in m.group("id") else m.group("id")},
                                   m.group("region"))
    m = _AGENT_ARN.match(s)
    if m:
        aliases = [{"agentAliasId": m.group("alias")}] if m.group("alias") else []
        return agent_candidate({"agentId": m.group("id"), "agentName": m.group("id")}, m.group("region"),
                               m.group("account"), aliases)
    m = _ENGINE.match(s)
    if m:
        return engine_candidate({"name": f"projects/{m.group('project')}/locations/{m.group('location')}"
                                         f"/reasoningEngines/{m.group('id')}"})
    return None


def looks_like_ref(ref: str) -> bool:
    return parse_ref(ref) is not None


# --------------------------------------------------------------------------- AWS
NO_AWS_CREDENTIAL = ("no AWS credential: export AWS_PROFILE (or AWS_ACCESS_KEY_ID and "
                     "AWS_SECRET_ACCESS_KEY), or run `aws sso login`")
AWS_CREDENTIAL_REJECTED = ("AWS credential rejected ({code}): refresh it — `aws sso login`, or re-export "
                           "AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY and AWS_SESSION_TOKEN")
NO_AWS_CLIENT = ("no AWS client on this machine: pip install 'ascend-cli[aws]' (boto3), or install the AWS CLI")
_REJECTED_CODES = ("InvalidClientTokenId", "UnrecognizedClientException", "ExpiredToken", "ExpiredTokenException",
                   "InvalidSignatureException", "AuthFailure", "RequestExpired")


def _boto3():
    try:
        import boto3  # noqa: PLC0415  (lazy: the aws extra)
        return boto3
    except Exception:  # noqa: BLE001 - absent or broken is the same answer: not this path
        return None


def _boto3_clients(profile: Optional[str] = None) -> Callable[[str, str], Any]:
    b3 = _boto3()
    if b3 is None:
        raise CloudError(NO_AWS_CLIENT, "no_aws_client")
    session = b3.Session(profile_name=profile or os.environ.get("AWS_PROFILE") or None)

    def make(service: str, region: str):
        return session.client(service, region_name=region)
    return make


def _classify_aws_exception(exc: BaseException) -> CloudError:
    name = type(exc).__name__
    text = str(exc)
    code = ""
    resp = getattr(exc, "response", None)
    if isinstance(resp, dict):
        code = str(((resp.get("Error") or {}).get("Code")) or "")
    if name in ("NoCredentialsError", "PartialCredentialsError", "CredentialRetrievalError") or "Unable to locate credentials" in text:
        return CloudError(NO_AWS_CREDENTIAL, "no_aws_credential")
    if code in _REJECTED_CODES or any(c in text for c in _REJECTED_CODES) or "security token included in the request is invalid" in text:
        return CloudError(AWS_CREDENTIAL_REJECTED.format(code=code or name), "aws_credential_rejected")
    if name == "EndpointConnectionError" or "Could not connect to the endpoint" in text:
        return CloudError(f"AWS endpoint unreachable: {text[:160]}", "aws_unreachable")
    return CloudError(f"AWS error ({code or name}): {text[:200]}", "aws_error")


def _pages(call: Callable[..., Dict[str, Any]], key: str, **kw: Any) -> List[Dict[str, Any]]:
    """Every page of a boto3 list call that pages on `nextToken`."""
    out: List[Dict[str, Any]] = []
    token = None
    for _ in range(50):
        got = call(**({**kw, "nextToken": token} if token else kw)) or {}
        out.extend(got.get(key) or [])
        token = got.get("nextToken")
        if not token:
            break
    return out


def aws_list(regions: List[str], *, make_client: Optional[Callable[[str, str], Any]] = None,
             run: Optional[Callable[[List[str]], Tuple[int, str, str]]] = None,
             prefer: str = "auto", profile: Optional[str] = None,
             account: Optional[str] = None) -> Dict[str, Any]:
    """The AgentCore runtimes and classic Bedrock agents the credential can see, per region.

    `make_client(service, region)` is the boto3 seam and `run(argv)` the CLI seam; tests inject
    both, the command injects neither. `prefer` is auto | boto3 | cli. Read-only: five list calls
    and nothing else. An AccessDenied in one region is an `errors` row, and the other regions
    still answer; a credential that does not exist or is rejected stops everything with one line.
    """
    regions = [r for r in (regions or []) if r]
    if not regions:
        raise CloudError("pass --region (e.g. --region us-east-2); AWS lists per region", "no_region")
    via = None
    if prefer in ("auto", "boto3") and (make_client is not None or _boto3() is not None):
        via = "boto3"
        make = make_client or _boto3_clients(profile)
    elif prefer in ("auto", "cli") and (run is not None or shutil.which("aws")):
        via = "aws-cli"
    else:
        raise CloudError(NO_AWS_CLIENT, "no_aws_client")

    out: Dict[str, Any] = {"platform": PLATFORM_BEDROCK, "via": via, "regions": regions, "candidates": [], "errors": []}
    acct = account or ""
    for region in regions:
        try:
            if via == "boto3":
                if not acct:
                    try:
                        acct = str(make("sts", region).get_caller_identity().get("Account") or "")
                    except Exception as exc:  # noqa: BLE001 - classified below
                        raise _classify_aws_exception(exc)
                control = make("bedrock-agentcore-control", region)
                for rt in _pages(control.list_agent_runtimes, "agentRuntimes", maxResults=100):
                    out["candidates"].append(agentcore_candidate(rt, region))
                agents = make("bedrock-agent", region)
                for ag in _pages(agents.list_agents, "agentSummaries", maxResults=100):
                    aliases = _pages(agents.list_agent_aliases, "agentAliasSummaries", agentId=ag.get("agentId"), maxResults=100)
                    out["candidates"].append(agent_candidate(ag, region, acct, aliases))
            else:
                runner = run or _run_aws
                if not acct:
                    acct = str(_aws_json(runner, ["sts", "get-caller-identity"], region).get("Account") or "")
                for rt in _aws_json(runner, ["bedrock-agentcore-control", "list-agent-runtimes"], region).get("agentRuntimes") or []:
                    out["candidates"].append(agentcore_candidate(rt, region))
                for ag in _aws_json(runner, ["bedrock-agent", "list-agents"], region).get("agentSummaries") or []:
                    aliases = _aws_json(runner, ["bedrock-agent", "list-agent-aliases", "--agent-id", str(ag.get("agentId"))],
                                        region).get("agentAliasSummaries") or []
                    out["candidates"].append(agent_candidate(ag, region, acct, aliases))
        except CloudError as ce:
            if ce.code in ("no_aws_credential", "aws_credential_rejected", "no_aws_client"):
                raise
            out["errors"].append({"region": region, "error": str(ce), "code": ce.code})
        except Exception as exc:  # noqa: BLE001 - boto3 raises a family of exceptions; one classifier
            ce = _classify_aws_exception(exc)
            if ce.code in ("no_aws_credential", "aws_credential_rejected"):
                raise ce
            out["errors"].append({"region": region, "error": str(ce), "code": ce.code})
    out["account"] = acct
    return out


def _run_aws(argv: List[str]) -> Tuple[int, str, str]:
    try:
        p = subprocess.run(["aws", *argv], capture_output=True, text=True, timeout=120)
    except FileNotFoundError:
        raise CloudError(NO_AWS_CLIENT, "no_aws_client")
    except subprocess.TimeoutExpired:
        raise CloudError("the aws CLI did not answer within 120 s", "aws_timeout")
    return p.returncode, p.stdout, p.stderr


def _aws_json(run: Callable[[List[str]], Tuple[int, str, str]], argv: List[str], region: str) -> Dict[str, Any]:
    code, out, err = run([*argv, "--region", region, "--output", "json"])
    if code != 0:
        text = (err or out or "").strip()
        if "Unable to locate credentials" in text:
            raise CloudError(NO_AWS_CREDENTIAL, "no_aws_credential")
        for c in _REJECTED_CODES:
            if c in text:
                raise CloudError(AWS_CREDENTIAL_REJECTED.format(code=c), "aws_credential_rejected")
        if "security token included in the request is invalid" in text:
            raise CloudError(AWS_CREDENTIAL_REJECTED.format(code="InvalidClientTokenId"), "aws_credential_rejected")
        raise CloudError(f"aws {' '.join(argv[:2])} failed: {text[:200]}", "aws_error")
    try:
        return json.loads(out or "{}")
    except json.JSONDecodeError:
        raise CloudError(f"aws {' '.join(argv[:2])} printed something that is not JSON", "aws_error")


# --------------------------------------------------------------------------- GCP
NO_GCP_CREDENTIAL = "no GCP credential: run `gcloud auth login` (or set GOOGLE_OAUTH_ACCESS_TOKEN)"
NO_GCLOUD = ("no gcloud on PATH: install the Google Cloud SDK and run `gcloud auth login`, "
             "or set GOOGLE_OAUTH_ACCESS_TOKEN")
GCP_CREDENTIAL_REJECTED = "GCP credential rejected (HTTP {status}): run `gcloud auth login` and try again"


def gcp_token(*, run: Optional[Callable[[List[str]], Tuple[int, str, str]]] = None,
              env: Optional[Dict[str, str]] = None) -> str:
    """The bearer for the Vertex REST list: the environment's, else the one gcloud mints. The
    value is returned to the caller that will put it on a request and never printed."""
    e = os.environ if env is None else env
    tok = str(e.get("GOOGLE_OAUTH_ACCESS_TOKEN") or "").strip()
    if tok:
        return tok
    if run is None:
        if not shutil.which("gcloud"):
            raise CloudError(NO_GCLOUD, "no_gcloud")
        run = _run_gcloud
    code, out, err = run(["auth", "print-access-token"])
    tok = (out or "").strip().splitlines()[-1].strip() if (out or "").strip() else ""
    if code != 0 or not tok:
        raise CloudError(NO_GCP_CREDENTIAL, "no_gcp_credential")
    return tok


def _run_gcloud(argv: List[str]) -> Tuple[int, str, str]:
    try:
        p = subprocess.run(["gcloud", *argv], capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        raise CloudError(NO_GCLOUD, "no_gcloud")
    except subprocess.TimeoutExpired:
        raise CloudError("gcloud did not answer within 60 s", "gcloud_timeout")
    return p.returncode, p.stdout, p.stderr


def gcp_list(project: str, regions: List[str], *, token: Optional[str] = None,
             opener: Optional[Callable[..., Any]] = None,
             run: Optional[Callable[[List[str]], Tuple[int, str, str]]] = None) -> Dict[str, Any]:
    """The reasoning engines in `project` per location, from the Vertex REST list, with the
    streamQuery endpoint each one answers on. `opener(request, timeout=…)` is the urllib seam."""
    project = str(project or "").strip()
    regions = [r for r in (regions or []) if r]
    if not project:
        raise CloudError("pass --project (the GCP project id that holds the Agent Engine)", "no_project")
    if not regions:
        raise CloudError("pass --region (the Vertex location, e.g. --region us-east4)", "no_region")
    bearer = token or gcp_token(run=run)
    opn = opener or urllib.request.urlopen
    out: Dict[str, Any] = {"platform": PLATFORM_GCP, "via": "rest", "project": project, "regions": regions,
                           "candidates": [], "errors": []}
    for location in regions:
        page = None
        try:
            for _ in range(50):
                q = {"pageSize": "100"}
                if page:
                    q["pageToken"] = page
                url = (f"https://{location}-aiplatform.googleapis.com/v1/projects/{project}/locations/{location}"
                       f"/reasoningEngines?{urllib.parse.urlencode(q)}")
                req = urllib.request.Request(url, headers={"Authorization": f"Bearer {bearer}", "Accept": "application/json"})
                with opn(req, timeout=60) as r:
                    body = json.loads(r.read().decode("utf-8") or "{}")
                for eng in body.get("reasoningEngines") or []:
                    out["candidates"].append(engine_candidate(eng))
                page = body.get("nextPageToken")
                if not page:
                    break
        except urllib.error.HTTPError as he:
            if he.code in (401, 403):
                raise CloudError(GCP_CREDENTIAL_REJECTED.format(status=he.code), "gcp_credential_rejected")
            detail = ""
            try:
                detail = str((json.loads(he.read().decode("utf-8") or "{}").get("error") or {}).get("message") or "")[:160]
            except Exception:  # noqa: BLE001 - the status is the fact; the body is a nicety
                detail = ""
            out["errors"].append({"region": location, "error": f"HTTP {he.code}{(' ' + detail) if detail else ''}",
                                  "code": "gcp_http_error"})
        except urllib.error.URLError as ue:
            out["errors"].append({"region": location, "error": f"unreachable: {str(ue.reason)[:120]}", "code": "gcp_unreachable"})
    return out


# --------------------------------------------------------------------------- state
def _norm(s: Any) -> str:
    return " ".join(str(s or "").split()).lower()


def same_name(a: Any, b: Any) -> bool:
    """The console's name join: exact after whitespace and case are folded; empty never matches."""
    return bool(_norm(a)) and _norm(a) == _norm(b)


def _norm_url(u: Any) -> str:
    s = str(u or "").strip()
    if s.startswith("https://") and ":streamQuery" in s:
        s = s.split("?", 1)[0]
    return s


def _tag_values(row: Dict[str, Any]) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}
    for t in row.get("tags") or []:
        if isinstance(t, dict):
            out.setdefault(str(t.get("group") or ""), []).append(str(t.get("value") or ""))
    return out


def discover_matches(cand: Dict[str, Any], rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The Discover rows this candidate is, by the console's own join for a homegrown agent — the
    name — narrowed by what the row says about where it lives (its source tag; for an engine,
    the `account` and `region` tags the GCP connector writes)."""
    want = _norm(cand.get("name"))
    if not want:
        return []
    hits = []
    for r in rows or []:
        if not isinstance(r, dict) or r.get("killed"):
            continue
        if _norm(r.get("label")) != want:
            continue
        tags = _tag_values(r)
        src = tags.get("source") or []
        expected = SOURCE_TAGS.get(str(cand.get("kind") or ""))
        if src and expected and expected not in src:
            continue
        if cand.get("kind") == "engine":
            acct = tags.get("account") or []
            if acct and cand.get("project") and cand["project"] not in acct:
                continue
            reg = tags.get("region") or []
            if reg and cand.get("region") and cand["region"] not in reg:
                continue
        hits.append({"id": str(r.get("id") or ""), "label": str(r.get("label") or ""),
                     "vendor": str(r.get("vendor") or ""), "has_activity": bool(r.get("has_activity"))})
    return hits


def state_of(cand: Dict[str, Any], apps: Optional[List[Dict[str, Any]]], rows: Optional[List[Dict[str, Any]]]) -> Dict[str, Any]:
    """{state, app, discover, checked}. `apps` and `rows` None means the platform was not read
    (no PAT): the state is then unknown rather than claimed, and `checked` says so."""
    if apps is None and rows is None:
        return {"state": None, "app": None, "discover": None, "checked": False}
    app = None
    want = _norm_url(cand.get("url"))
    for a in apps or []:
        if isinstance(a, dict) and want and _norm_url(a.get("url")) == want:
            app = {"id": str(a.get("id") or ""), "name": str(a.get("name") or ""), "api_type": str(a.get("api_type") or "")}
            break
    hits = discover_matches(cand, rows or [])
    if app:
        state = ONBOARDED
    elif hits:
        state = TESTABLE
    else:
        state = CANDIDATE
    return {"state": state, "app": app, "discover": (hits[0] if hits else None),
            "discover_matches": len(hits), "checked": True}


def unmatched_rows(kind_rows: List[Dict[str, Any]], candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Discover rows of a cloud platform that no listed candidate matched: potential · needs access
    (an account or region the credential does not reach, or a resource since deleted)."""
    seen = set()
    for c in candidates:
        for h in discover_matches(c, kind_rows):
            seen.add(h["id"])
    out = []
    for r in kind_rows:
        if not isinstance(r, dict) or r.get("killed") or str(r.get("id") or "") in seen:
            continue
        out.append({"id": str(r.get("id") or ""), "label": str(r.get("label") or ""), "state": NEEDS_ACCESS})
    return out


def platform_rows(rows: List[Dict[str, Any]], platform: str) -> List[Dict[str, Any]]:
    """The Discover rows imported from one cloud platform (by source tag; never by vendor alone,
    which also names a model vendor's log-ingest rows)."""
    want = {PLATFORM_BEDROCK: {"bedrock-agentcore", "bedrock"}, PLATFORM_GCP: {"gcp-agent-platform"}}.get(platform, set())
    out = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        if set(_tag_values(r).get("source") or []) & want:
            out.append(r)
    return out


# --------------------------------------------------------------------------- the spec
MASK = "••••"


def _env_ref(value: Optional[str], env: Optional[Dict[str, str]] = None) -> Optional[str]:
    """`env:NAME` names a variable so a secret never sits on a command line; a literal passes."""
    if not value:
        return value
    s = str(value)
    if s.startswith("env:"):
        e = os.environ if env is None else env
        got = e.get(s[4:], "")
        if not got:
            raise CloudError(f"{s} names an environment variable that is not set", "env_ref_unset")
        return got
    return s


def spec_kwargs(cand: Dict[str, Any], *, name: str, auth: Optional[str] = None, role_arn: Optional[str] = None,
                external_id: Optional[str] = None, role_session_name: Optional[str] = None,
                access_key_id: Optional[str] = None, secret_access_key: Optional[str] = None,
                session_token: Optional[str] = None, region: Optional[str] = None,
                service_account_info: Optional[str] = None, system_prompt: Optional[str] = None,
                business_purpose: Optional[str] = None, env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """The keyword arguments `api.build_app_spec` needs for this candidate, with the per-method
    requirement named here — before any request — rather than as a 422 from the platform."""
    if not name or not str(name).strip():
        raise CloudError("an application name is required (--name, or the candidate's own name)", "no_name")
    base = {"name": str(name).strip(), "system_prompt": system_prompt or None, "business_purpose": business_purpose or None}
    if cand.get("platform") == PLATFORM_GCP:
        if not service_account_info:
            raise CloudError("a 'gcp' application needs the service account Ascend will call the engine with: "
                             "--service-account @/path/to/sa.json (a path you name; it is read once, never printed)",
                             "service_account_required")
        try:
            parsed = json.loads(service_account_info)
        except (TypeError, ValueError):
            raise CloudError("--service-account must be the service-account JSON (or @path to it)", "service_account_invalid")
        if not isinstance(parsed, dict) or parsed.get("type") != "service_account":
            raise CloudError("--service-account is not a service-account key (expected \"type\": \"service_account\")",
                             "service_account_invalid")
        return {**base, "api_type": "gcp", "url": cand["url"], "service_account_info": service_account_info}
    method = (auth or "assume-role").strip().lower()
    if method not in AUTH_METHODS:
        raise CloudError(f"--auth must be one of: {', '.join(AUTH_METHODS)}", "auth_invalid")
    kw: Dict[str, Any] = {**base, "api_type": "bedrock", "url": cand["url"], "bedrock_authentication_method": method,
                          "region": region or cand.get("region") or None}
    if method == "assume-role":
        if not role_arn:
            raise CloudError("assume-role needs --role-arn: the IAM role in your account that Ascend assumes to invoke "
                             "the agent (it must trust Straiker's cross-account role, carry the external id you pass "
                             "with --external-id, and allow bedrock-agentcore:InvokeAgentRuntime or bedrock:InvokeAgent)",
                             "role_arn_required")
        kw.update({"role_arn": role_arn, "external_id": _env_ref(external_id, env), "role_session_name": role_session_name or None})
    else:
        ak, sk = _env_ref(access_key_id, env), _env_ref(secret_access_key, env)
        if not ak or not sk:
            raise CloudError("access-key needs --access-key-id and --secret-access-key (a literal, or env:NAME so the "
                             "value never sits on the command line)", "access_key_required")
        kw.update({"access_key_id": ak, "secret_access_key": sk, "session_token": _env_ref(session_token, env)})
    return {k: v for k, v in kw.items() if v is not None}


def masked(spec: Dict[str, Any]) -> Dict[str, Any]:
    """The spec as it may be printed: every credential value replaced, nothing else touched."""
    out = dict(spec)
    # The external id is the confused-deputy check on the role's trust policy: shared with
    # Straiker on purpose, and still nothing a terminal scrollback or a transcript should hold.
    for k in ("secret_access_key", "session_token", "service_account_info", "api_key", "thin_api_key", "external_id"):
        if out.get(k):
            out[k] = MASK
    return out


# --------------------------------------------------------------------------- the local check
def check_config(cand: Dict[str, Any], *, name: str, sa_key_file: Optional[str] = None,
                 discover: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The CLI's own adapter config for `ascend target check <name>`: the bedrock adapter in
    agentcore or agent mode, or the vertex_ai adapter on the streamQuery endpoint — on THIS
    machine's credentials (the AWS chain; a named key file, else ADC). It carries no secret."""
    base = {"_source": "cloud", "_platform": cand.get("platform"), "_kind": cand.get("kind"),
            "_discover": ({"id": discover.get("id"), "label": discover.get("label")} if discover else None),
            "_note": ("proven from this machine on its own cloud credentials; the platform reaches the target "
                      "with the credentials on the application record, which this check does not exercise")}
    if cand.get("platform") == PLATFORM_GCP:
        cfg = {"adapter": "vertex_ai", "endpoint": cand["url"], "url": cand["url"], **base}
        if sa_key_file:
            cfg["sa_key_file"] = sa_key_file
        return cfg
    if cand.get("kind") == "agent":
        cfg = {"adapter": "bedrock", "mode": "agent", "agent_id": cand.get("id"), "region": cand.get("region"),
               "url": cand["url"], **base}
        if cand.get("alias"):
            cfg["agent_alias_id"] = cand["alias"]
        return cfg
    return {"adapter": "bedrock", "mode": "agentcore", "runtime_arn": cand["url"], "region": cand.get("region"),
            "url": cand["url"], **base}


def slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(name or "").lower()).strip("-")
    return (s[:48] or "cloud-target") + ".cloud"

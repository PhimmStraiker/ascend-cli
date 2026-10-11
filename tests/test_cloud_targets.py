"""
`ascend target cloud` — the agents the operator's own cloud credentials can see, as native targets.

Offline. The cloud answers are the shapes `ListAgentRuntimes`, `ListAgents`, `ListAgentAliases`
and the Vertex `reasoningEngines` list returned on 2026-10-10 (account, names and ids replaced by
stand-ins); the Discover rows are the inventory's own shape from the same day. What must hold:

  * a reference is parsed for what it is — an AgentCore runtime ARN, a classic agent ARN with or
    without its alias, an engine resource name or its REST / streamQuery URL — and each becomes
    the Ascend `url` the platform's `bedrock` / `gcp` schema takes;
  * AWS is read through boto3 when it is there and the aws CLI otherwise, both paged, and the
    result says which; a missing or rejected credential is ONE plain line naming what to set, and
    an AccessDenied in one region does not hide the others;
  * GCP is read through the Vertex REST list with the token gcloud mints or the environment
    carries, paged on `nextPageToken`, and no token ever reaches a candidate or a message;
  * the state words are the console's and nothing else: onboarded when an application carries the
    ARN or endpoint, potential · testable now with your cloud credentials when a Discover row of
    that platform has the name, cloud candidate otherwise; a row no candidate matched is potential
    · needs access; with no PAT nothing is claimed;
  * the spec for both types is built locally with the per-method requirement named (a role to
    assume, a key pair, a service-account key), `env:NAME` keeps a value off the command line, and
    everything printed under --dry-run is masked;
  * through the real parser: `list` prints the table and the JSON, `add --dry-run` sends nothing,
    `add` creates the record the platform schema needs, stores the local record and the check
    config, and `--match` names the Discover row or refuses.

Run:  python3 -m pytest tests/test_cloud_targets.py -q
"""
from __future__ import annotations

import importlib.util
import io
import json
import sys
import urllib.error
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
for _p in (REPO / "runtime", REPO / "control", REPO / "shells" / "cli"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import api  # noqa: E402
import cloud_targets as CT  # noqa: E402

_spec = importlib.util.spec_from_file_location("ascend_cli_cloud", REPO / "shells" / "cli" / "ascend.py")
cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli)

ACCT = "123456789012"
RT_ARN = f"arn:aws:bedrock-agentcore:us-east-2:{ACCT}:runtime/lab_support_agent-AbCdEf1234"
RT2_ARN = f"arn:aws:bedrock-agentcore:us-east-2:{ACCT}:runtime/lab_planner-GhIjKl5678"
AG_ARN = f"arn:aws:bedrock:us-east-2:{ACCT}:agent/AGENT12345"
ENGINE = "projects/lab-project/locations/us-east4/reasoningEngines/1234567890123456789"
ENGINE_URL = ("https://us-east4-aiplatform.googleapis.com/v1/projects/lab-project/locations/us-east4"
              "/reasoningEngines/1234567890123456789:streamQuery?alt=sse")

# ListAgentRuntimes, as boto3 returns it (2026-10-10), two pages.
RUNTIMES_P1 = {"agentRuntimes": [
    {"agentRuntimeArn": RT_ARN, "agentRuntimeId": "lab_support_agent-AbCdEf1234", "agentRuntimeVersion": "1",
     "agentRuntimeName": "lab_support_agent", "lastUpdatedAt": "2026-09-18T04:08:10.479788+00:00", "status": "READY"}],
    "nextToken": "p2"}
RUNTIMES_P2 = {"agentRuntimes": [
    {"agentRuntimeArn": RT2_ARN, "agentRuntimeId": "lab_planner-GhIjKl5678", "agentRuntimeVersion": "16",
     "agentRuntimeName": "lab_planner", "description": "Planner", "lastUpdatedAt": "2026-10-04T14:07:49.713886+00:00",
     "status": "READY"}]}
AGENTS = {"agentSummaries": [
    {"agentId": "AGENT12345", "agentName": "Demo-Agent", "agentStatus": "PREPARED", "description": "Demo",
     "updatedAt": "2025-12-09T00:27:21.133748+00:00"}]}
ALIASES = {"agentAliasSummaries": [
    {"agentAliasId": "ALIAS12345", "agentAliasName": "prod", "routingConfiguration": [{"agentVersion": "1"}],
     "agentAliasStatus": "PREPARED"},
    {"agentAliasId": "TSTALIASID", "agentAliasName": "AgentTestAlias", "routingConfiguration": [{"agentVersion": "DRAFT"}],
     "agentAliasStatus": "PREPARED"}]}
ENGINES_P1 = {"reasoningEngines": [
    {"name": ENGINE, "displayName": "support-bot-v1", "createTime": "2026-09-16T22:00:00Z",
     "updateTime": "2026-09-16T22:10:00Z"}], "nextPageToken": "t2"}
ENGINES_P2 = {"reasoningEngines": [
    {"name": "projects/lab-project/locations/us-east4/reasoningEngines/9876543210987654321", "displayName": "probe-agent"}]}

# Discover rows: the inventory's own shape (2026-10-10), ids replaced.
def _row(label, aid, source, vendor="aws", extra_tags=(), killed=False):
    tags = [{"group": "source", "value": source}] + [{"group": g, "value": v} for g, v in extra_tags]
    return {"id": aid, "object": "inventory.agent", "label": label, "vendor": vendor, "type": "autonomous_agent",
            "tags": tags, "killed": killed, "has_activity": False, "sources": [{"source": "vendor_inventory"}]}


ROWS = [
    _row("lab_support_agent", "agt_support000000001", "bedrock-agentcore"),
    _row("Demo-Agent", "agt_demo0000000000002", "bedrock"),
    _row("other_runtime", "agt_other000000000003", "bedrock-agentcore"),
    _row("support-bot-v1", "agt_engine0000000004", "gcp-agent-platform", vendor="google",
         extra_tags=(("account", "lab-project"), ("region", "us-east4"), ("framework", "google-adk"))),
    _row("lab_planner", "agt_killed0000000005", "bedrock-agentcore", killed=True),
    _row("lab_planner", "agt_logs00000000006", "bedrock-logingest"),
    {"id": "agt_agentforce0000007", "label": "Service Agent", "vendor": "salesforce", "tags": [{"group": "source", "value": "agentforce"}]},
]
APPS = [{"id": "aapp_onboarded000001", "name": "planner-lab", "api_type": "bedrock", "url": RT2_ARN}]


# --------------------------------------------------------------------------- fakes
class FakeAWS:
    """boto3 clients from a table, recording every call; `deny` makes one region AccessDenied."""

    def __init__(self, deny_region=None, raise_on_sts=None):
        self.calls, self.deny_region, self.raise_on_sts = [], deny_region, raise_on_sts

    def __call__(self, service, region):
        fake = self
        deny = region == self.deny_region

        class _Client:
            def get_caller_identity(self):
                fake.calls.append(("sts", region))
                if fake.raise_on_sts:
                    raise fake.raise_on_sts
                return {"Account": ACCT}

            def list_agent_runtimes(self, **kw):
                fake.calls.append(("list_agent_runtimes", region, kw.get("nextToken")))
                if deny:
                    raise _client_error("AccessDeniedException", "not authorized")
                return RUNTIMES_P2 if kw.get("nextToken") == "p2" else RUNTIMES_P1

            def list_agents(self, **kw):
                fake.calls.append(("list_agents", region, kw.get("nextToken")))
                return AGENTS

            def list_agent_aliases(self, **kw):
                fake.calls.append(("list_agent_aliases", region, kw.get("agentId")))
                return ALIASES
        return _Client()


class _ClientError(Exception):
    def __init__(self, code, msg):
        super().__init__(f"An error occurred ({code}) when calling the operation: {msg}")
        self.response = {"Error": {"Code": code, "Message": msg}}


def _client_error(code, msg):
    return _ClientError(code, msg)


class NoCredentialsError(Exception):
    pass


def fake_aws_cli(answers):
    calls = []

    def run(argv):
        calls.append(argv)
        key = " ".join(argv[:2])
        a = answers.get(key)
        if a is None:
            return 254, "", f"\nAn error occurred (AccessDeniedException) when calling {key}"
        if isinstance(a, tuple):
            return a
        return 0, json.dumps(a), ""
    run.calls = calls
    return run


class FakeOpener:
    """urllib.request.urlopen for the Vertex list: pages by `pageToken`, records the bearer."""

    def __init__(self, status=None):
        self.requests, self.status = [], status

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        if self.status:
            raise urllib.error.HTTPError(req.full_url, self.status, "nope", {}, io.BytesIO(b'{"error":{"message":"denied"}}'))
        body = ENGINES_P2 if "pageToken=t2" in req.full_url else ENGINES_P1

        class _R:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                return False

            def read(self_inner):
                return json.dumps(body).encode()
        return _R()


# --------------------------------------------------------------------------- references
class TestParseRef:
    def test_an_agentcore_runtime_arn_is_a_bedrock_candidate_whose_url_is_the_arn(self):
        c = CT.parse_ref(RT_ARN)
        assert c["platform"] == "bedrock" and c["kind"] == "agentcore" and c["url"] == RT_ARN
        assert c["region"] == "us-east-2" and c["account"] == ACCT and c["name"] == "lab_support_agent"

    def test_a_classic_agent_arn_with_and_without_its_alias(self):
        c = CT.parse_ref(AG_ARN)
        assert c["kind"] == "agent" and c["url"] == AG_ARN and c["id"] == "AGENT12345" and c["alias"] == ""
        c2 = CT.parse_ref(f"arn:aws:bedrock:us-east-2:{ACCT}:agent-alias/AGENT12345/ALIAS12345")
        assert c2["url"] == AG_ARN and c2["alias"] == "ALIAS12345"

    @pytest.mark.parametrize("ref", [ENGINE, f"https://us-east4-aiplatform.googleapis.com/v1/{ENGINE}",
                                     f"https://us-east4-aiplatform.googleapis.com/v1/{ENGINE}:streamQuery?alt=sse",
                                     f"{ENGINE}:streamQuery"])
    def test_an_engine_in_any_of_its_spellings_becomes_the_streamquery_endpoint(self, ref):
        c = CT.parse_ref(ref)
        assert c["platform"] == "gcp" and c["kind"] == "engine" and c["ref"] == ENGINE and c["url"] == ENGINE_URL
        assert c["project"] == "lab-project" and c["region"] == "us-east4"

    @pytest.mark.parametrize("ref", ["lab_support_agent", "", "arn:aws:lambda:us-east-2:123456789012:function:x",
                                     "https://chat.example.test/api", "arn:aws:bedrock-agentcore:us-east-2:12:runtime/x"])
    def test_anything_else_is_not_a_reference(self, ref):
        assert CT.parse_ref(ref) is None and not CT.looks_like_ref(ref)


# --------------------------------------------------------------------------- AWS
class TestAwsList:
    def test_boto3_pages_both_lists_and_says_so(self):
        fake = FakeAWS()
        out = CT.aws_list(["us-east-2"], make_client=fake)
        assert out["via"] == "boto3" and out["account"] == ACCT and out["errors"] == []
        names = [(c["kind"], c["name"]) for c in out["candidates"]]
        assert names == [("agentcore", "lab_support_agent"), ("agentcore", "lab_planner"), ("agent", "Demo-Agent")]
        assert ("list_agent_runtimes", "us-east-2", "p2") in fake.calls, "the second page was fetched"
        agent = out["candidates"][2]
        assert agent["url"] == AG_ARN and agent["alias"] == "ALIAS12345" and [a["name"] for a in agent["aliases"]] == ["prod", "AgentTestAlias"]
        assert out["candidates"][0]["status"] == "READY" and out["candidates"][0]["region"] == "us-east-2"

    def test_a_denied_region_is_an_error_row_and_the_others_still_answer(self):
        out = CT.aws_list(["us-west-2", "us-east-2"], make_client=FakeAWS(deny_region="us-west-2"))
        assert [e["region"] for e in out["errors"]] == ["us-west-2"] and "AccessDenied" in out["errors"][0]["error"]
        assert {c["region"] for c in out["candidates"]} == {"us-east-2"}

    def test_no_credential_is_one_plain_line_naming_what_to_set(self):
        with pytest.raises(CT.CloudError) as e:
            CT.aws_list(["us-east-2"], make_client=FakeAWS(raise_on_sts=NoCredentialsError("Unable to locate credentials")))
        assert e.value.code == "no_aws_credential" and str(e.value) == CT.NO_AWS_CREDENTIAL
        assert "AWS_PROFILE" in str(e.value) and "aws sso login" in str(e.value) and "\n" not in str(e.value)

    def test_a_rejected_credential_names_the_code_and_the_fix(self):
        with pytest.raises(CT.CloudError) as e:
            CT.aws_list(["us-east-2"], make_client=FakeAWS(raise_on_sts=_client_error("InvalidClientTokenId", "bad")))
        assert e.value.code == "aws_credential_rejected" and "InvalidClientTokenId" in str(e.value) and "aws sso login" in str(e.value)

    def test_no_region_is_refused_before_any_call(self):
        with pytest.raises(CT.CloudError) as e:
            CT.aws_list([], make_client=FakeAWS())
        assert e.value.code == "no_region" and "--region" in str(e.value)

    def test_the_aws_cli_path_reads_the_same_shapes_and_says_so(self):
        run = fake_aws_cli({"sts get-caller-identity": {"Account": ACCT},
                            "bedrock-agentcore-control list-agent-runtimes": {"agentRuntimes": RUNTIMES_P1["agentRuntimes"] + RUNTIMES_P2["agentRuntimes"]},
                            "bedrock-agent list-agents": AGENTS, "bedrock-agent list-agent-aliases": ALIASES})
        out = CT.aws_list(["us-east-2"], run=run, prefer="cli")
        assert out["via"] == "aws-cli" and [c["name"] for c in out["candidates"]] == ["lab_support_agent", "lab_planner", "Demo-Agent"]
        assert all("--region" in argv and "--output" in argv for argv in run.calls)
        assert any(argv[:4] == ["bedrock-agent", "list-agent-aliases", "--agent-id", "AGENT12345"] for argv in run.calls)

    def test_the_aws_cli_path_turns_a_credential_failure_into_the_same_line(self):
        run = fake_aws_cli({"sts get-caller-identity": (253, "", "Unable to locate credentials. You can configure credentials by running \"aws configure\".")})
        with pytest.raises(CT.CloudError) as e:
            CT.aws_list(["us-east-2"], run=run, prefer="cli")
        assert e.value.code == "no_aws_credential"
        run = fake_aws_cli({"sts get-caller-identity": (254, "", "An error occurred (ExpiredToken) when calling the GetCallerIdentity operation: The security token included in the request is expired")})
        with pytest.raises(CT.CloudError) as e:
            CT.aws_list(["us-east-2"], run=run, prefer="cli")
        assert e.value.code == "aws_credential_rejected" and "ExpiredToken" in str(e.value)

    def test_without_boto3_or_the_cli_the_line_names_both_installs(self, monkeypatch):
        monkeypatch.setattr(CT, "_boto3", lambda: None)
        monkeypatch.setattr(CT.shutil, "which", lambda name: None)
        with pytest.raises(CT.CloudError) as e:
            CT.aws_list(["us-east-2"])
        assert e.value.code == "no_aws_client" and "ascend-cli[aws]" in str(e.value) and "AWS CLI" in str(e.value)


# --------------------------------------------------------------------------- GCP
class TestGcpList:
    def test_the_rest_list_is_paged_and_each_engine_gets_its_streamquery_endpoint(self):
        opener = FakeOpener()
        out = CT.gcp_list("lab-project", ["us-east4"], token="tok-secret", opener=opener)
        assert out["via"] == "rest" and [c["name"] for c in out["candidates"]] == ["support-bot-v1", "probe-agent"]
        assert out["candidates"][0]["url"] == ENGINE_URL and out["candidates"][0]["ref"] == ENGINE
        assert len(opener.requests) == 2 and "pageToken=t2" in opener.requests[1].full_url
        assert all(r.get_header("Authorization") == "Bearer tok-secret" for r in opener.requests)
        assert "tok-secret" not in json.dumps(out), "the token never reaches a candidate"

    def test_the_token_comes_from_the_environment_or_gcloud_and_is_never_printed(self):
        assert CT.gcp_token(env={"GOOGLE_OAUTH_ACCESS_TOKEN": "env-tok"}, run=lambda argv: (1, "", "x")) == "env-tok"
        assert CT.gcp_token(env={}, run=lambda argv: (0, "minted-tok\n", "")) == "minted-tok"
        with pytest.raises(CT.CloudError) as e:
            CT.gcp_token(env={}, run=lambda argv: (1, "", "ERROR: (gcloud.auth.print-access-token) Reauthentication failed"))
        assert e.value.code == "no_gcp_credential" and str(e.value) == CT.NO_GCP_CREDENTIAL and "\n" not in str(e.value)

    def test_a_rejected_token_and_a_missing_project_or_region_are_plain_lines(self):
        with pytest.raises(CT.CloudError) as e:
            CT.gcp_list("lab-project", ["us-east4"], token="t", opener=FakeOpener(status=401))
        assert e.value.code == "gcp_credential_rejected" and "401" in str(e.value) and "gcloud auth login" in str(e.value)
        with pytest.raises(CT.CloudError) as e:
            CT.gcp_list("", ["us-east4"], token="t", opener=FakeOpener())
        assert e.value.code == "no_project"
        with pytest.raises(CT.CloudError) as e:
            CT.gcp_list("lab-project", [], token="t", opener=FakeOpener())
        assert e.value.code == "no_region"
        out = CT.gcp_list("lab-project", ["us-east4"], token="t", opener=FakeOpener(status=404))
        assert out["candidates"] == [] and out["errors"][0]["error"].startswith("HTTP 404")


# --------------------------------------------------------------------------- state
class TestState:
    def test_the_three_words_and_nothing_else(self):
        rows = CT.platform_rows(ROWS, "bedrock")
        assert {r["label"] for r in rows} == {"lab_support_agent", "Demo-Agent", "other_runtime", "lab_planner"}
        support = CT.state_of(CT.parse_ref(RT_ARN), APPS, rows)
        assert support["state"] == CT.TESTABLE and support["discover"]["id"] == "agt_support000000001" and support["app"] is None
        planner = CT.state_of(CT.agentcore_candidate(RUNTIMES_P2["agentRuntimes"][0], "us-east-2"), APPS, rows)
        assert planner["state"] == CT.ONBOARDED and planner["app"]["id"] == "aapp_onboarded000001"
        stranger = CT.state_of(CT.agentcore_candidate({"agentRuntimeArn": f"arn:aws:bedrock-agentcore:us-east-2:{ACCT}:runtime/nobody-Xy", "agentRuntimeName": "nobody"}, "us-east-2"), APPS, rows)
        assert stranger["state"] == CT.CANDIDATE and stranger["discover"] is None
        assert {support["state"], planner["state"], stranger["state"]} <= set(CT.STATES)

    def test_a_name_match_respects_the_source_tag_and_skips_killed_rows(self):
        rows = CT.platform_rows(ROWS, "bedrock")
        # lab_planner: the only live row with that name is a log-ingest row, which platform_rows drops.
        assert CT.discover_matches(CT.agentcore_candidate(RUNTIMES_P2["agentRuntimes"][0], "us-east-2"), rows) == []
        # a classic agent matches a `bedrock` row, not an agentcore one — by the NAME the listing
        # carries; a bare ARN names nothing (parse_ref falls back to the id), so it matches nothing
        agent = CT.agent_candidate(AGENTS["agentSummaries"][0], "us-east-2", ACCT, ALIASES["agentAliasSummaries"])
        assert CT.discover_matches(agent, rows)[0]["id"] == "agt_demo0000000000002"
        assert CT.discover_matches(CT.parse_ref(AG_ARN), rows) == []
        assert CT.discover_matches(CT.agentcore_candidate({"agentRuntimeArn": RT_ARN, "agentRuntimeName": "Demo-Agent"}, "us-east-2"), rows) == []

    def test_an_engine_matches_its_row_by_name_project_and_region(self):
        rows = CT.platform_rows(ROWS, "gcp")
        c = CT.engine_candidate(ENGINES_P1["reasoningEngines"][0])
        assert CT.discover_matches(c, rows)[0]["id"] == "agt_engine0000000004"
        other = dict(c, project="other-project")
        assert CT.discover_matches(other, rows) == []
        # a bare resource name carries no display name: it is the id until a listing names it
        assert CT.parse_ref(ENGINE)["name"] == "1234567890123456789" and CT.discover_matches(CT.parse_ref(ENGINE), rows) == []

    def test_rows_no_candidate_matched_are_needs_access_and_no_pat_claims_nothing(self):
        rows = CT.platform_rows(ROWS, "bedrock")
        needs = CT.unmatched_rows(rows, [CT.parse_ref(RT_ARN)])
        assert {n["label"] for n in needs} == {"Demo-Agent", "other_runtime"} and all(n["state"] == CT.NEEDS_ACCESS for n in needs)
        unknown = CT.state_of(CT.parse_ref(RT_ARN), None, None)
        assert unknown == {"state": None, "app": None, "discover": None, "checked": False}

    def test_the_endpoint_join_ignores_the_sse_query_only(self):
        rows = CT.platform_rows(ROWS, "gcp")
        c = CT.engine_candidate(ENGINES_P1["reasoningEngines"][0])
        apps = [{"id": "aapp_gcp", "name": "support", "api_type": "gcp", "url": ENGINE_URL.split("?")[0]}]
        assert CT.state_of(c, apps, rows)["state"] == CT.ONBOARDED
        assert CT.state_of(c, [{"id": "x", "url": ENGINE_URL.replace("1234567890123456789", "1")}], rows)["state"] == CT.TESTABLE


# --------------------------------------------------------------------------- the spec
class TestSpec:
    def test_assume_role_needs_the_role_and_builds_the_platform_schema(self):
        c = CT.parse_ref(RT_ARN)
        with pytest.raises(CT.CloudError) as e:
            CT.spec_kwargs(c, name="lab")
        assert e.value.code == "role_arn_required" and "--role-arn" in str(e.value) and "InvokeAgentRuntime" in str(e.value)
        kw = CT.spec_kwargs(c, name="lab", role_arn=f"arn:aws:iam::{ACCT}:role/StraikerAscend",
                            external_id="env:EXT", env={"EXT": "ext-123"})
        spec = api.build_app_spec(**kw)
        assert spec["api_type"] == "bedrock" and spec["url"] == RT_ARN and spec["bedrock_authentication_method"] == "assume-role"
        assert spec["role_arn"].endswith("role/StraikerAscend") and spec["external_id"] == "ext-123" and spec["region"] == "us-east-2"
        assert "access_key_id" not in spec and "service_account_info" not in spec
        assert set(api.REQUIRED_BY_TYPE["bedrock"]) <= set(spec)

    def test_access_key_needs_both_halves_and_env_refs_keep_them_off_the_line(self):
        c = CT.parse_ref(AG_ARN)
        with pytest.raises(CT.CloudError) as e:
            CT.spec_kwargs(c, name="lab", auth="access-key", access_key_id="AKIA")
        assert e.value.code == "access_key_required"
        kw = CT.spec_kwargs(c, name="lab", auth="access-key", access_key_id="env:AK", secret_access_key="env:SK",
                            session_token="env:ST", env={"AK": "AKIAEXAMPLE", "SK": "sekret", "ST": "tok"})
        spec = api.build_app_spec(**kw)
        assert (spec["access_key_id"], spec["secret_access_key"], spec["session_token"]) == ("AKIAEXAMPLE", "sekret", "tok")
        with pytest.raises(CT.CloudError) as e:
            CT.spec_kwargs(c, name="lab", auth="access-key", access_key_id="env:MISSING", secret_access_key="x", env={})
        assert e.value.code == "env_ref_unset" and "MISSING" in str(e.value)
        with pytest.raises(CT.CloudError) as e:
            CT.spec_kwargs(c, name="lab", auth="password")
        assert e.value.code == "auth_invalid"

    def test_gcp_needs_a_service_account_key_and_takes_the_streamquery_endpoint(self):
        c = CT.parse_ref(ENGINE)
        with pytest.raises(CT.CloudError) as e:
            CT.spec_kwargs(c, name="bot")
        assert e.value.code == "service_account_required" and "@/path" in str(e.value)
        with pytest.raises(CT.CloudError) as e:
            CT.spec_kwargs(c, name="bot", service_account_info='{"type": "authorized_user"}')
        assert e.value.code == "service_account_invalid"
        sa = json.dumps({"type": "service_account", "project_id": "lab-project", "private_key": "-----BEGIN PRIVATE KEY-----\nabc\n"})
        spec = api.build_app_spec(**CT.spec_kwargs(c, name="bot", service_account_info=sa))
        assert spec["api_type"] == "gcp" and spec["url"] == ENGINE_URL and spec["service_account_info"] == sa
        assert set(api.REQUIRED_BY_TYPE["gcp"]) <= set(spec)

    def test_masked_hides_every_credential_and_nothing_else(self):
        spec = {"name": "x", "url": RT_ARN, "secret_access_key": "s", "session_token": "t", "external_id": "e",
                "service_account_info": "{}", "role_arn": "arn:aws:iam::1:role/r", "api_key": "k"}
        m = CT.masked(spec)
        assert {m[k] for k in ("secret_access_key", "session_token", "external_id", "service_account_info", "api_key")} == {CT.MASK}
        assert m["role_arn"] == spec["role_arn"] and m["url"] == RT_ARN and m["name"] == "x"
        assert spec["secret_access_key"] == "s", "the caller's spec is untouched"

    def test_the_check_config_is_the_adapter_config_for_this_machine_and_carries_no_secret(self):
        cfg = CT.check_config(CT.parse_ref(RT_ARN), name="lab", discover={"id": "agt_1", "label": "lab_support_agent"})
        assert cfg["adapter"] == "bedrock" and cfg["mode"] == "agentcore" and cfg["runtime_arn"] == RT_ARN and cfg["region"] == "us-east-2"
        assert cfg["_discover"] == {"id": "agt_1", "label": "lab_support_agent"} and cfg["_source"] == "cloud"
        agent = CT.check_config(CT.parse_ref(f"arn:aws:bedrock:us-east-2:{ACCT}:agent-alias/AGENT12345/ALIAS12345"), name="d")
        assert agent["mode"] == "agent" and agent["agent_id"] == "AGENT12345" and agent["agent_alias_id"] == "ALIAS12345"
        gcp = CT.check_config(CT.parse_ref(ENGINE), name="bot", sa_key_file="/keys/sa.json")
        assert gcp["adapter"] == "vertex_ai" and gcp["endpoint"] == ENGINE_URL and gcp["sa_key_file"] == "/keys/sa.json"
        assert "service_account_info" not in json.dumps(gcp) and "private_key" not in json.dumps(gcp)
        assert CT.slug("Agentcore Harness Lab") == "agentcore-harness-lab.cloud"


# --------------------------------------------------------------------------- through the parser
class Recorder:
    def __init__(self, apps=None, rows=None, inventory_error=None):
        self.apps, self.rows, self.inventory_error = list(apps or []), list(rows if rows is not None else ROWS), inventory_error
        self.created, self.listed = [], 0

    def list_apps(self):
        self.listed += 1
        return {"data": list(self.apps)}

    def list_inventory_agents(self, **kw):
        if self.inventory_error:
            raise self.inventory_error
        return list(self.rows)

    def list_controls(self):
        return {"controls": [{"id": "sys_prompt_leak"}, {"id": "pii_leak"}]}

    def validate_controls(self, ids):
        return api.AscendAPI.validate_controls(self, ids)

    def create_app(self, spec):
        self.created.append(json.loads(json.dumps(spec)))
        app = {"id": f"aapp_new{len(self.created)}", "name": spec["name"], "api_type": spec["api_type"], "url": spec["url"]}
        self.apps.append(app)
        return app

    def get_app(self, app_id):
        return next((a for a in self.apps if a["id"] == app_id), None)


def run(monkeypatch, rec, *argv, json_mode=False, listing=None, token="s6r_pat_test"):
    argv = list(argv) + (["--json"] if json_mode else [])
    monkeypatch.setattr(cli, "_client", lambda args, **kw: rec)
    monkeypatch.setattr(cli, "_validated_control_ids", lambda c, ids, **kw: ids)
    monkeypatch.setattr(cli, "_resolve_all_controls", lambda c, args, ctrl: ctrl or ["sys_prompt_leak", "pii_leak"])
    if listing is not None:
        monkeypatch.setattr(CT, "aws_list", lambda regions, **kw: listing)
        monkeypatch.setattr(CT, "gcp_list", lambda project, regions, **kw: listing)
    if token:
        monkeypatch.setenv("STRAIKER_PAT", token)
    else:
        monkeypatch.delenv("STRAIKER_PAT", raising=False)
        monkeypatch.delenv("STRAIKER_TOKEN", raising=False)
    monkeypatch.setattr(sys, "argv", ["ascend", *argv])
    ns = cli.build_parser().parse_args(argv)
    cli._reapply_globals(ns, argv)
    ns.func(ns)


def exits_with(monkeypatch, rec, *argv, **kw):
    with pytest.raises(SystemExit) as e:
        run(monkeypatch, rec, *argv, **kw)
    return e.value.code


LISTING = {"platform": "bedrock", "via": "boto3", "regions": ["us-east-2"], "account": ACCT, "errors": [],
           "candidates": [CT.agentcore_candidate(RUNTIMES_P1["agentRuntimes"][0], "us-east-2"),
                          CT.agentcore_candidate(RUNTIMES_P2["agentRuntimes"][0], "us-east-2"),
                          CT.agent_candidate(AGENTS["agentSummaries"][0], "us-east-2", ACCT, ALIASES["agentAliasSummaries"])]}


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("ASCEND_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("ASCEND_CONFIG_DIR", str(tmp_path / "configs"))
    monkeypatch.setattr(cli, "config_dir", lambda: tmp_path / "configs")
    import creds as C
    monkeypatch.setattr(C, "store_path", lambda: tmp_path / "home" / "keys.json")
    return tmp_path


class TestListCommand:
    def test_the_table_carries_the_state_words_and_the_refs(self, monkeypatch, capsys, home):
        run(monkeypatch, Recorder(apps=APPS), "target", "cloud", "list", "--aws", "--region", "us-east-2", listing=LISTING)
        out = capsys.readouterr().out
        assert "lab_support_agent" in out and CT.TESTABLE in out and "agt_support000000001" in out
        assert "lab_planner" in out and CT.ONBOARDED in out and "aapp_onboarded000001" in out
        assert f"ref {RT_ARN}" in out and "3 candidate(s) via boto3" in out
        assert CT.NEEDS_ACCESS in out and "other_runtime" in out
        assert "register one:  ascend target cloud add" in out and "--role-arn" in out

    def test_json_is_the_listing_with_state_and_discover_per_candidate(self, monkeypatch, capsys, home):
        run(monkeypatch, Recorder(apps=APPS), "target", "cloud", "list", "--aws", "--region", "us-east-2", listing=LISTING, json_mode=True)
        d = json.loads(capsys.readouterr().out)
        assert d["via"] == "boto3" and d["platform_checked"] is True
        by = {c["name"]: c for c in d["candidates"]}
        assert by["lab_support_agent"]["state"] == CT.TESTABLE and by["lab_support_agent"]["discover"]["id"] == "agt_support000000001"
        assert by["lab_planner"]["state"] == CT.ONBOARDED and by["lab_planner"]["app"]["id"] == "aapp_onboarded000001"
        assert by["Demo-Agent"]["state"] == CT.TESTABLE and {n["label"] for n in d["needs_access"]} == {"other_runtime"}

    def test_without_a_pat_nothing_is_claimed(self, monkeypatch, capsys, home):
        run(monkeypatch, Recorder(), "target", "cloud", "list", "--aws", "--region", "us-east-2", listing=LISTING, json_mode=True, token=None)
        err = capsys.readouterr()
        d = json.loads(err.out)
        assert d["platform_checked"] is False and all(c["state"] is None for c in d["candidates"]) and d["needs_access"] == []
        assert "no PAT" in err.err

    def test_which_cloud_is_required_and_a_cloud_error_is_the_json_envelope(self, monkeypatch, capsys, home):
        assert exits_with(monkeypatch, Recorder(), "target", "cloud", "list", "--region", "us-east-2", json_mode=True) == cli.EXIT_USAGE
        monkeypatch.setattr(CT, "aws_list", lambda regions, **kw: (_ for _ in ()).throw(CT.CloudError(CT.NO_AWS_CREDENTIAL, "no_aws_credential")))
        assert exits_with(monkeypatch, Recorder(), "target", "cloud", "list", "--aws", "--region", "us-east-2", json_mode=True) == cli.EXIT_ERROR
        d = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert d["ok"] is False and d["error"]["code"] == "no_aws_credential" and d["error"]["message"] == CT.NO_AWS_CREDENTIAL

    def test_an_inventory_the_pat_cannot_read_still_lists_with_onboarded_or_candidate(self, monkeypatch, capsys, home):
        run(monkeypatch, Recorder(apps=APPS, inventory_error=RuntimeError("403")), "target", "cloud", "list", "--aws", "--region", "us-east-2",
            listing=LISTING, json_mode=True)
        got = capsys.readouterr()
        by = {c["name"]: c["state"] for c in json.loads(got.out)["candidates"]}
        assert by == {"lab_support_agent": CT.CANDIDATE, "lab_planner": CT.ONBOARDED, "Demo-Agent": CT.CANDIDATE}
        assert "inventory not readable" in got.err


class TestAddCommand:
    ROLE = f"arn:aws:iam::{ACCT}:role/StraikerAscend"

    def test_dry_run_prints_the_masked_spec_and_sends_nothing(self, monkeypatch, capsys, home):
        monkeypatch.setenv("EXT", "ext-secret-value")
        rec = Recorder()
        run(monkeypatch, rec, "target", "cloud", "add", RT_ARN, "--name", "support-lab", "--role-arn", self.ROLE,
            "--external-id", "env:EXT", "--dry-run", json_mode=True)
        d = json.loads(capsys.readouterr().out)
        assert d["dry_run"] is True and d["sent"] is False and rec.created == []
        assert d["spec"]["api_type"] == "bedrock" and d["spec"]["url"] == RT_ARN and d["spec"]["role_arn"] == self.ROLE
        assert d["spec"]["external_id"] == CT.MASK and "ext-secret-value" not in json.dumps(d)
        assert d["discover"]["id"] == "agt_support000000001" and d["state_after"] == CT.ONBOARDED
        assert "support-lab" in d["join_note"] and "lab_support_agent" in d["join_note"]
        rec2 = Recorder()
        run(monkeypatch, rec2, "target", "cloud", "add", RT_ARN, "--role-arn", self.ROLE, "--dry-run")
        text = capsys.readouterr().out
        assert "dry run — nothing sent" in text and "lab_support_agent" in text and rec2.created == []

    def test_a_real_add_creates_the_native_app_stores_the_record_and_writes_the_check_config(self, monkeypatch, capsys, home):
        import creds as C
        rec = Recorder()
        run(monkeypatch, rec, "target", "cloud", "add", RT_ARN, "--name", "support-lab", "--role-arn", self.ROLE,
            "--external-id", "ext-1", "--controls", "sys_prompt_leak", json_mode=True)
        d = json.loads(capsys.readouterr().out)
        assert len(rec.created) == 1
        spec = rec.created[0]
        assert spec["api_type"] == "bedrock" and spec["url"] == RT_ARN and spec["bedrock_authentication_method"] == "assume-role"
        assert spec["role_arn"] == self.ROLE and spec["external_id"] == "ext-1" and spec["region"] == "us-east-2"
        assert spec["control_ids"] == ["sys_prompt_leak"] and spec["control_type"] == "custom" and spec["max_queries_per_minute"] == 20
        assert d["created"] is True and d["app_id"] == "aapp_new1" and d["state"] == CT.ONBOARDED and d["needs_bridge"] is False
        assert d["discover"]["label"] == "lab_support_agent" and d["config"] == "support-lab.cloud"
        rec_stored = C.get("aapp_new1")
        assert rec_stored["app_name"] == "support-lab" and rec_stored["config"] == "support-lab.cloud" and rec_stored["adapter"] == "bedrock"
        assert rec_stored["thin_api_key"] is None, "a native app has no bridge key"
        cfg = json.loads((home / "configs" / "support-lab.cloud.json").read_text())
        assert cfg["mode"] == "agentcore" and cfg["runtime_arn"] == RT_ARN and cfg["_ascend"]["app_id"] == "aapp_new1"
        assert "ext-1" not in json.dumps(cfg), "the check config carries no credential"

    def test_the_human_record_reads_like_target_add(self, monkeypatch, capsys, home):
        run(monkeypatch, Recorder(), "target", "cloud", "add", RT_ARN, "--role-arn", self.ROLE)
        out = capsys.readouterr().out
        assert "target 'lab_support_agent' is ready" in out and "app       aapp_new1" in out
        assert "type      bedrock — Ascend calls it itself" in out and f"url       {RT_ARN}" in out
        assert "discover  lab_support_agent (agt_support000000001) — onboarded" in out
        assert "check     ascend target check 'lab_support_agent'" in out and "run it    ascend assess run --app 'lab_support_agent'" in out
        assert "note      the console joins" not in out, "named after its row, so the board joins them"

    def test_match_names_the_row_or_refuses(self, monkeypatch, capsys, home):
        rec = Recorder()
        run(monkeypatch, rec, "target", "cloud", "add", RT_ARN, "--role-arn", self.ROLE, "--match", "other_runtime", json_mode=True)
        d = json.loads(capsys.readouterr().out)
        assert d["discover"] == {"id": "agt_other000000000003", "label": "other_runtime"} and d["target"] == "other_runtime"
        assert rec.created[0]["name"] == "other_runtime"
        assert exits_with(monkeypatch, Recorder(), "target", "cloud", "add", RT_ARN, "--role-arn", self.ROLE, "--match", "no-such-row", json_mode=True) == cli.EXIT_USAGE
        d = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert d["error"]["code"] == "discover_row_not_found" and "no-such-row" in d["error"]["message"]

    def test_an_arn_already_on_an_application_is_refused_unless_reused(self, monkeypatch, capsys, home):
        assert exits_with(monkeypatch, Recorder(apps=APPS), "target", "cloud", "add", RT2_ARN, "--role-arn", self.ROLE, json_mode=True) == cli.EXIT_USAGE
        d = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert d["error"]["code"] == "already_onboarded" and "aapp_onboarded000001" in d["error"]["message"]
        rec = Recorder(apps=APPS)
        run(monkeypatch, rec, "target", "cloud", "add", RT2_ARN, "--name", "planner-lab", "--role-arn", self.ROLE, "--if-not-exists", json_mode=True)
        d = json.loads(capsys.readouterr().out)
        assert d["reused"] is True and d["app_id"] == "aapp_onboarded000001" and rec.created == []

    def test_a_missing_role_is_named_locally_and_a_gcp_target_needs_a_named_key_file(self, monkeypatch, capsys, home):
        assert exits_with(monkeypatch, Recorder(), "target", "cloud", "add", RT_ARN, json_mode=True) == cli.EXIT_USAGE
        d = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert d["error"]["code"] == "role_arn_required" and "--role-arn" in d["error"]["message"]
        assert exits_with(monkeypatch, Recorder(), "target", "cloud", "add", ENGINE, json_mode=True) == cli.EXIT_USAGE
        d = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert d["error"]["code"] == "service_account_required"
        sa = home / "sa.json"
        sa.write_text(json.dumps({"type": "service_account", "project_id": "lab-project", "private_key": "PK"}))
        rec = Recorder()
        # A bare resource name carries no display name, so the row is named with --match (or the
        # name is resolved by `--gcp --project` listing); the key file is read because it was named.
        run(monkeypatch, rec, "target", "cloud", "add", ENGINE, "--service-account", f"@{sa}", "--match", "support-bot-v1", json_mode=True)
        d = json.loads(capsys.readouterr().out)
        spec = rec.created[0]
        assert spec["api_type"] == "gcp" and spec["url"] == ENGINE_URL and json.loads(spec["service_account_info"])["project_id"] == "lab-project"
        assert d["target"] == "support-bot-v1" and d["discover"]["id"] == "agt_engine0000000004"
        cfg = json.loads((home / "configs" / "support-bot-v1.cloud.json").read_text())
        assert cfg["adapter"] == "vertex_ai" and cfg["sa_key_file"] == str(sa) and "PK" not in json.dumps(cfg)

    def test_a_bare_name_is_resolved_by_listing_and_an_unknown_one_refused(self, monkeypatch, capsys, home):
        rec = Recorder()
        run(monkeypatch, rec, "target", "cloud", "add", "lab_support_agent", "--aws", "--region", "us-east-2", "--role-arn", self.ROLE,
            "--dry-run", listing=LISTING, json_mode=True)
        d = json.loads(capsys.readouterr().out)
        assert d["url"] == RT_ARN and d["sent"] is False
        assert exits_with(monkeypatch, Recorder(), "target", "cloud", "add", "nobody", "--aws", "--region", "us-east-2", "--role-arn", self.ROLE,
                          listing=LISTING, json_mode=True) == cli.EXIT_USAGE
        assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["error"]["code"] == "candidate_not_found"
        assert exits_with(monkeypatch, Recorder(), "target", "cloud", "add", "nobody", "--role-arn", self.ROLE, json_mode=True) == cli.EXIT_USAGE
        assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["error"]["code"] == "unknown_candidate"

    def test_without_a_pat_a_real_add_dies_on_the_token_and_a_dry_run_still_prints(self, monkeypatch, capsys, home):
        monkeypatch.setattr(cli, "_client", cli._client.__wrapped__ if hasattr(cli._client, "__wrapped__") else cli._client)
        monkeypatch.delenv("STRAIKER_PAT", raising=False)
        monkeypatch.setattr(sys, "argv", ["ascend", "target", "cloud", "add", RT_ARN, "--role-arn", self.ROLE, "--json"])
        ns = cli.build_parser().parse_args(["target", "cloud", "add", RT_ARN, "--role-arn", self.ROLE, "--json"])
        cli._reapply_globals(ns, ["--json"])
        with pytest.raises(SystemExit) as e:
            ns.func(ns)
        assert e.value.code == cli.EXIT_USAGE and "no token" in json.loads(capsys.readouterr().out.strip().splitlines()[-1])["error"]["message"]
        ns = cli.build_parser().parse_args(["target", "cloud", "add", RT_ARN, "--role-arn", self.ROLE, "--dry-run", "--json"])
        cli._reapply_globals(ns, ["--json"])
        ns.func(ns)
        d = json.loads(capsys.readouterr().out)
        assert d["sent"] is False and d["discover"] is None and d["note"] and "no PAT" in d["note"]

"""Tests for security and correctness hardening changes."""

import json

import httpx
import pytest
import respx

from mcp_zuul.classifier import classify_failure
from mcp_zuul.formatters import _compute_chain_summary, fmt_buildset, fmt_status_item
from mcp_zuul.helpers import (
    AppContext,
    _pick_client,
    api,
    api_delete,
    api_post,
    fetch_log_url,
    parse_zuul_url,
    stream_log,
)
from mcp_zuul.parsers import parse_playbooks, smart_truncate
from mcp_zuul.server import _BearerAuth
from mcp_zuul.tools import list_jobs, list_projects
from mcp_zuul.tools._config import list_nodes
from mcp_zuul.tools._logjuicer import get_build_anomalies
from mcp_zuul.tools._logs import browse_build_logs, get_build_log, tail_build_log
from tests.conftest import make_build

# -- parse_zuul_url single-tenant support --


class TestParseZuulUrlSingleTenant:
    def test_build_url_without_tenant(self):
        result = parse_zuul_url("https://zuul.example.com/build/abc123")
        assert result == ("", "build", "abc123")

    def test_buildset_url_without_tenant(self):
        result = parse_zuul_url("https://zuul.example.com/buildset/def456")
        assert result == ("", "buildset", "def456")

    def test_multi_tenant_takes_priority(self):
        """Multi-tenant /t/ pattern must match before single-tenant fallback."""
        result = parse_zuul_url("https://zuul.example.com/t/tenant/build/abc123")
        assert result == ("tenant", "build", "abc123")

    def test_single_tenant_with_path_prefix(self):
        result = parse_zuul_url("https://zuul.example.com/zuul/build/abc123")
        assert result == ("", "build", "abc123")

    def test_single_tenant_with_query_params(self):
        result = parse_zuul_url("https://zuul.example.com/build/abc123?tab=logs")
        assert result == ("", "build", "abc123")

    def test_change_url_still_requires_tenant(self):
        """Change status URLs only work with /t/ prefix — no single-tenant fallback."""
        result = parse_zuul_url("https://zuul.example.com/status/change/12345,abc")
        assert result is None


# -- _BearerAuth --


class TestBearerAuth:
    def test_adds_authorization_header(self):
        auth = _BearerAuth("my-token")
        request = httpx.Request("GET", "https://zuul.example.com/api/tenants")
        flow = auth.auth_flow(request)
        modified = next(flow)
        assert modified.headers["Authorization"] == "Bearer my-token"

    def test_is_httpx_auth_subclass(self):
        """httpx.Auth subclass ensures auth is stripped on cross-origin redirects."""
        auth = _BearerAuth("secret")
        assert isinstance(auth, httpx.Auth)

    def test_different_tokens(self):
        auth1 = _BearerAuth("token-a")
        auth2 = _BearerAuth("token-b")
        req1 = httpx.Request("GET", "https://a.com")
        req2 = httpx.Request("GET", "https://b.com")
        assert next(auth1.auth_flow(req1)).headers["Authorization"] == "Bearer token-a"
        assert next(auth2.auth_flow(req2)).headers["Authorization"] == "Bearer token-b"


# -- api() non-JSON response --


class TestApiNonJsonResponse:
    @respx.mock
    async def test_non_json_200_raises_value_error(self, mock_ctx):
        """Reverse proxy returning HTML 200 should give a clear error."""
        respx.get("https://zuul.example.com/api/tenants").mock(
            return_value=httpx.Response(
                200,
                text="<html><body>Maintenance</body></html>",
                headers={"content-type": "text/html"},
            )
        )
        with pytest.raises(ValueError, match="non-JSON response"):
            await api(mock_ctx, "/tenants")

    @respx.mock
    async def test_non_json_error_includes_content_type(self, mock_ctx):
        respx.get("https://zuul.example.com/api/tenants").mock(
            return_value=httpx.Response(
                200,
                text="not json",
                headers={"content-type": "text/plain"},
            )
        )
        with pytest.raises(ValueError, match="text/plain"):
            await api(mock_ctx, "/tenants")


class TestApiPostNonJsonResponse:
    @respx.mock
    async def test_non_json_200_raises_value_error(self, mock_ctx):
        """Reverse proxy returning HTML 200 on POST should give a clear error."""
        respx.post("https://zuul.example.com/api/tenant/test-tenant/project/org/repo/enqueue").mock(
            return_value=httpx.Response(
                200,
                text="<html>Maintenance</html>",
                headers={"content-type": "text/html"},
            )
        )
        with pytest.raises(ValueError, match="non-JSON response"):
            await api_post(
                mock_ctx,
                "/tenant/test-tenant/project/org/repo/enqueue",
                {"pipeline": "check", "change": "123,1"},
            )

    @respx.mock
    async def test_empty_response_returns_empty_dict(self, mock_ctx):
        respx.post("https://zuul.example.com/api/tenant/test-tenant/project/org/repo/enqueue").mock(
            return_value=httpx.Response(200, text="")
        )
        result = await api_post(
            mock_ctx,
            "/tenant/test-tenant/project/org/repo/enqueue",
            {"pipeline": "check", "change": "123,1"},
        )
        assert result == {}


class TestApiDeleteNonJsonResponse:
    @respx.mock
    async def test_non_json_200_raises_value_error(self, mock_ctx):
        respx.delete("https://zuul.example.com/api/tenant/test-tenant/autohold/ah-1").mock(
            return_value=httpx.Response(
                200,
                text="<html>Error</html>",
                headers={"content-type": "text/html"},
            )
        )
        with pytest.raises(ValueError, match="non-JSON response"):
            await api_delete(mock_ctx, "/tenant/test-tenant/autohold/ah-1")


# -- _pick_client --


class TestPickClient:
    def test_same_host_returns_auth_client(self, config):
        client = httpx.AsyncClient(base_url="https://zuul.example.com")
        log_client = httpx.AsyncClient()
        ctx = AppContext(client=client, log_client=log_client, config=config)
        result = _pick_client(ctx, "https://zuul.example.com/logs/build/file.txt")
        assert result is client

    def test_different_host_returns_log_client(self, config):
        client = httpx.AsyncClient(base_url="https://zuul.example.com")
        log_client = httpx.AsyncClient()
        ctx = AppContext(client=client, log_client=log_client, config=config)
        result = _pick_client(ctx, "https://logs.external.com/build/file.txt")
        assert result is log_client


# -- stream_log truncation --


class TestStreamLogTruncation:
    @respx.mock
    async def test_returns_truncated_flag_false_for_small_log(self, mock_ctx):
        a = mock_ctx.request_context.lifespan_context
        respx.get("https://logs.example.com/build/log.txt").mock(
            return_value=httpx.Response(200, content=b"small log content")
        )
        content, truncated = await stream_log(a, "https://logs.example.com/build/log.txt")
        assert content == b"small log content"
        assert truncated is False

    @respx.mock
    async def test_returns_truncated_flag_true_for_large_log(self, mock_ctx):
        a = mock_ctx.request_context.lifespan_context
        # Create content larger than 10 MB
        large_content = b"x" * (11 * 1024 * 1024)
        respx.get("https://logs.example.com/build/log.txt").mock(
            return_value=httpx.Response(200, content=large_content)
        )
        content, truncated = await stream_log(a, "https://logs.example.com/build/log.txt")
        assert truncated is True
        assert len(content) == 10 * 1024 * 1024  # exactly 10 MB

    @respx.mock
    async def test_404_raises_file_not_found(self, mock_ctx):
        a = mock_ctx.request_context.lifespan_context
        respx.get("https://logs.example.com/build/missing.txt").mock(
            return_value=httpx.Response(404)
        )
        with pytest.raises(FileNotFoundError):
            await stream_log(a, "https://logs.example.com/build/missing.txt")


# -- fetch_log_url streaming cap --


class TestFetchLogUrlStreaming:
    @respx.mock
    async def test_returns_response_for_small_file(self, mock_ctx):
        a = mock_ctx.request_context.lifespan_context
        respx.get("https://logs.example.com/build/file.json").mock(
            return_value=httpx.Response(200, content=b'{"key": "value"}')
        )
        resp = await fetch_log_url(a, "https://logs.example.com/build/file.json")
        assert resp.status_code == 200
        assert resp.content == b'{"key": "value"}'

    @respx.mock
    async def test_caps_large_download_at_20mb(self, mock_ctx):
        a = mock_ctx.request_context.lifespan_context
        large_content = b"x" * (25 * 1024 * 1024)  # 25 MB
        respx.get("https://logs.example.com/build/huge.json").mock(
            return_value=httpx.Response(200, content=large_content)
        )
        resp = await fetch_log_url(a, "https://logs.example.com/build/huge.json")
        assert resp.status_code == 200
        assert len(resp.content) == 20 * 1024 * 1024  # exactly 20 MB

    @respx.mock
    async def test_404_returns_empty_content(self, mock_ctx):
        a = mock_ctx.request_context.lifespan_context
        respx.get("https://logs.example.com/build/missing.json").mock(
            return_value=httpx.Response(404)
        )
        resp = await fetch_log_url(a, "https://logs.example.com/build/missing.json")
        assert resp.status_code == 404
        assert resp.content == b""


# -- list_jobs / list_projects limit --


class TestListJobsLimit:
    @respx.mock
    async def test_default_limit_truncates_large_result(self, mock_ctx):
        jobs = [{"name": f"job-{i}", "variants": [{}]} for i in range(250)]
        respx.get("https://zuul.example.com/api/tenant/test-tenant/jobs").mock(
            return_value=httpx.Response(200, json=jobs)
        )
        result = json.loads(await list_jobs(mock_ctx))
        assert result["count"] == 200
        assert result["total"] == 250
        assert result["truncated"] is True

    @respx.mock
    async def test_custom_limit(self, mock_ctx):
        jobs = [{"name": f"job-{i}", "variants": [{}]} for i in range(50)]
        respx.get("https://zuul.example.com/api/tenant/test-tenant/jobs").mock(
            return_value=httpx.Response(200, json=jobs)
        )
        result = json.loads(await list_jobs(mock_ctx, limit=10))
        assert result["count"] == 10
        assert result["total"] == 50
        assert result["truncated"] is True

    @respx.mock
    async def test_unlimited_with_zero(self, mock_ctx):
        jobs = [{"name": f"job-{i}", "variants": [{}]} for i in range(250)]
        respx.get("https://zuul.example.com/api/tenant/test-tenant/jobs").mock(
            return_value=httpx.Response(200, json=jobs)
        )
        result = json.loads(await list_jobs(mock_ctx, limit=0))
        assert result["count"] == 250
        assert "truncated" not in result

    @respx.mock
    async def test_no_truncation_flag_when_within_limit(self, mock_ctx):
        jobs = [{"name": f"job-{i}", "variants": [{}]} for i in range(5)]
        respx.get("https://zuul.example.com/api/tenant/test-tenant/jobs").mock(
            return_value=httpx.Response(200, json=jobs)
        )
        result = json.loads(await list_jobs(mock_ctx))
        assert result["count"] == 5
        assert "truncated" not in result
        assert "total" not in result


class TestListProjectsLimit:
    @respx.mock
    async def test_default_limit_truncates_large_result(self, mock_ctx):
        projects = [{"name": f"org/repo-{i}"} for i in range(250)]
        respx.get("https://zuul.example.com/api/tenant/test-tenant/projects").mock(
            return_value=httpx.Response(200, json=projects)
        )
        result = json.loads(await list_projects(mock_ctx))
        assert result["count"] == 200
        assert result["total"] == 250
        assert result["truncated"] is True

    @respx.mock
    async def test_no_truncation_when_within_limit(self, mock_ctx):
        projects = [{"name": f"org/repo-{i}"} for i in range(10)]
        respx.get("https://zuul.example.com/api/tenant/test-tenant/projects").mock(
            return_value=httpx.Response(200, json=projects)
        )
        result = json.loads(await list_projects(mock_ctx))
        assert result["count"] == 10
        assert "truncated" not in result


# -- fmt_build missing job_name --


class TestFmtBuildMissingJobName:
    def test_missing_job_name_uses_default(self):
        from mcp_zuul.formatters import fmt_build

        build = {"uuid": "u1", "result": "SUCCESS", "pipeline": "check"}
        result = fmt_build(build)
        assert result["job"] == "unknown"


# -- Kerberos auth SSL error hint --


class TestKerberosSSLError:
    def _mock_kerberos_config(self, mock_config):
        """Set up a mock Config for Kerberos lifespan tests."""
        cfg = mock_config.return_value
        cfg.base_url = "https://zuul.example.com"
        cfg.auth_token = None
        cfg.timeout = 30
        cfg.verify_ssl = True
        cfg.use_kerberos = True
        cfg.transport = "stdio"
        cfg.enabled_tools = None
        cfg.disabled_tools = None
        cfg.read_only = True
        cfg.logjuicer_url = None
        return cfg

    async def test_ssl_error_gives_actionable_hint(self):
        """Kerberos auth SSL failure should suggest ZUUL_VERIFY_SSL=false."""
        from unittest.mock import AsyncMock, patch

        from mcp_zuul.server import lifespan
        from tests._factories import make_ssl_connect_error

        mock_server = AsyncMock()
        with (
            patch("mcp_zuul.server.Config.from_env") as mock_config,
            patch("mcp_zuul.server.kerberos_auth") as mock_auth,
        ):
            self._mock_kerberos_config(mock_config)
            mock_auth.side_effect = make_ssl_connect_error()

            with pytest.raises(RuntimeError, match="ZUUL_VERIFY_SSL"):
                async with lifespan(mock_server):
                    pass

    async def test_non_ssl_connect_error_reraises(self):
        """Non-SSL ConnectError during Kerberos auth should re-raise as-is."""
        from unittest.mock import AsyncMock, patch

        from mcp_zuul.server import lifespan
        from tests._factories import make_connect_error

        mock_server = AsyncMock()
        with (
            patch("mcp_zuul.server.Config.from_env") as mock_config,
            patch("mcp_zuul.server.kerberos_auth") as mock_auth,
        ):
            self._mock_kerberos_config(mock_config)
            mock_auth.side_effect = make_connect_error("Connection refused")

            with pytest.raises(httpx.ConnectError, match="Connection refused"):
                async with lifespan(mock_server):
                    pass


# -- Kerberos auth None token guard --


class TestKerberosNoneTokenGuard:
    async def test_none_token_raises_runtime_error(self):
        from unittest.mock import MagicMock, patch

        mock_gssapi = MagicMock()
        mock_ctx = MagicMock()
        mock_ctx.step.return_value = None
        mock_gssapi.SecurityContext.return_value = mock_ctx
        mock_gssapi.Name.return_value = MagicMock()

        with patch.dict("sys.modules", {"gssapi": mock_gssapi}):
            from importlib import reload

            import mcp_zuul.auth as auth_mod

            reload(auth_mod)

            client = httpx.AsyncClient()
            # Mock the redirect chain to reach the 401 Negotiate stage
            with respx.mock:
                respx.get("https://zuul.example.com/api/tenants").mock(
                    return_value=httpx.Response(401, headers={"www-authenticate": "Negotiate"})
                )
                with pytest.raises(RuntimeError, match="produced no token"):
                    await auth_mod.kerberos_auth(client, "https://zuul.example.com")
            await client.aclose()


# -- list_nodes detail limit --


class TestListNodesLimit:
    @respx.mock
    async def test_detail_respects_limit(self, mock_ctx):
        nodes = [{"id": f"n-{i}", "type": ["centos"], "state": "ready"} for i in range(300)]
        respx.get("https://zuul.example.com/api/tenant/test-tenant/nodes").mock(
            return_value=httpx.Response(200, json=nodes)
        )
        result = json.loads(await list_nodes(mock_ctx, detail=True))
        assert len(result["nodes"]) == 200
        assert result["count"] == 300
        assert result["detail_truncated"] is True

    @respx.mock
    async def test_detail_custom_limit(self, mock_ctx):
        nodes = [{"id": f"n-{i}", "type": ["centos"], "state": "ready"} for i in range(50)]
        respx.get("https://zuul.example.com/api/tenant/test-tenant/nodes").mock(
            return_value=httpx.Response(200, json=nodes)
        )
        result = json.loads(await list_nodes(mock_ctx, detail=True, limit=10))
        assert len(result["nodes"]) == 10
        assert result["count"] == 50
        assert result["detail_truncated"] is True

    @respx.mock
    async def test_summary_always_covers_all_nodes(self, mock_ctx):
        nodes = [{"id": f"n-{i}", "type": ["centos"], "state": "ready"} for i in range(300)]
        respx.get("https://zuul.example.com/api/tenant/test-tenant/nodes").mock(
            return_value=httpx.Response(200, json=nodes)
        )
        result = json.loads(await list_nodes(mock_ctx, detail=True, limit=10))
        assert result["by_state"]["ready"] == 300


# -- parse_playbooks failed_tasks cap --


class TestParsePlaybooksFailedTasksCap:
    def test_caps_at_50_failed_tasks(self):
        # Build a job-output with 100 failed hosts
        tasks = [
            {
                "task": {"name": "failing-task"},
                "hosts": {
                    f"host-{i}": {
                        "failed": True,
                        "msg": f"error on host {i}",
                        "rc": 1,
                        "stderr": "",
                        "stdout": "",
                    }
                    for i in range(100)
                },
            }
        ]
        data = [
            {
                "phase": "run",
                "playbook": "deploy.yaml",
                "stats": {f"host-{i}": {"failures": 1} for i in range(100)},
                "plays": [{"play": {"name": "test"}, "tasks": tasks}],
            }
        ]
        _playbooks, failed_tasks = parse_playbooks(data)
        assert len(failed_tasks) == 50


# -- stream_log scheme validation --


class TestStreamLogSchemeValidation:
    async def test_rejects_non_http_scheme(self, mock_ctx):
        a = mock_ctx.request_context.lifespan_context
        with pytest.raises(ValueError, match="Invalid URL scheme"):
            await stream_log(a, "file:///etc/passwd")

    async def test_rejects_ftp_scheme(self, mock_ctx):
        a = mock_ctx.request_context.lifespan_context
        with pytest.raises(ValueError, match="Invalid URL scheme"):
            await stream_log(a, "ftp://evil.com/file.txt")


# -- LogJuicer report_id sanitization --


class TestLogjuicerReportIdSanitization:
    @respx.mock
    async def test_rejects_traversal_in_report_id(self, mock_ctx):
        from tests.conftest import make_build

        mock_ctx.request_context.lifespan_context.config.logjuicer_url = (
            "https://logjuicer.example.com"
        )
        build = make_build(uuid="lj-1")
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/lj-1").mock(
            return_value=httpx.Response(200, json=build)
        )
        respx.put("https://logjuicer.example.com/api/report/new").mock(
            return_value=httpx.Response(200, json={"id": "../../admin/delete"})
        )
        result = json.loads(await get_build_anomalies(mock_ctx, uuid="lj-1"))
        assert "error" in result
        assert "invalid" in result["error"].lower()


# -- _compute_chain_summary all_decided with unnamed jobs --


class TestChainSummaryAllDecidedUnnamedJobs:
    def test_unnamed_running_job_prevents_all_decided(self):
        """Unnamed running jobs must prevent all_decided from being True."""
        jobs = [
            {"name": "job-a", "result": "SUCCESS", "status": "SUCCESS"},
            {"result": None, "status": "RUNNING"},  # unnamed, still running
        ]
        summary = _compute_chain_summary(jobs)
        assert summary["all_decided"] is False

    def test_all_named_completed_is_decided(self):
        jobs = [
            {"name": "job-a", "result": "SUCCESS", "status": "SUCCESS"},
            {"name": "job-b", "result": "FAILURE", "status": "FAILURE"},
        ]
        summary = _compute_chain_summary(jobs)
        assert summary["all_decided"] is True


# -- parse_playbooks None host result --


class TestParsePlaybooksNoneHostResult:
    def test_none_host_result_skipped(self):
        """Null host result in job-output.json should be skipped, not crash."""
        data = [
            {
                "phase": "run",
                "playbook": "deploy.yaml",
                "stats": {"host-0": {"failures": 1}},
                "plays": [
                    {
                        "play": {"name": "test"},
                        "tasks": [
                            {
                                "task": {"name": "task-1"},
                                "hosts": {"host-0": None, "host-1": {"failed": True, "msg": "err"}},
                            }
                        ],
                    }
                ],
            }
        ]
        _playbooks, failed_tasks = parse_playbooks(data)
        assert len(failed_tasks) == 1
        assert failed_tasks[0]["host"] == "host-1"


# -- fmt_buildset non-dict refs --


class TestFmtBuildsetNonDictRefs:
    def test_string_ref_handled(self):
        bs = {
            "uuid": "bs-1",
            "result": "SUCCESS",
            "pipeline": "check",
            "refs": ["not-a-dict"],
        }
        result = fmt_buildset(bs)
        assert result["uuid"] == "bs-1"
        assert "project" not in result

    def test_none_ref_handled(self):
        bs = {
            "uuid": "bs-2",
            "result": "SUCCESS",
            "pipeline": "check",
            "refs": [None],
        }
        result = fmt_buildset(bs)
        assert result["uuid"] == "bs-2"


# -- classify_failure infra results without task data --


class TestClassifyFailureInfraResults:
    def test_node_failure_classified_as_infra(self):
        c = classify_failure("NODE_FAILURE", [], [])
        assert c.category == "INFRA_FLAKE"
        assert c.retryable is True
        assert "NODE_FAILURE" in c.reason

    def test_retry_limit_classified_as_infra(self):
        c = classify_failure("RETRY_LIMIT", [], [])
        assert c.category == "INFRA_FLAKE"
        assert c.retryable is True

    def test_disk_full_classified_as_infra(self):
        c = classify_failure("DISK_FULL", [], [])
        assert c.category == "INFRA_FLAKE"
        assert c.retryable is True

    def test_merger_failure_classified_as_config_error(self):
        c = classify_failure("MERGER_FAILURE", [], [])
        assert c.category == "CONFIG_ERROR"
        assert c.retryable is False


# -- refs[0] type guard --


class TestRefsTypeGuard:
    def test_fmt_status_item_with_non_dict_refs(self):
        """Non-dict refs elements must not crash fmt_status_item."""
        item = {
            "id": "123,1",
            "active": True,
            "live": True,
            "refs": ["some-string-ref"],
            "jobs": [],
        }
        result = fmt_status_item(item)
        assert "project" not in result
        assert "change" not in result

    def test_fmt_status_item_with_empty_refs(self):
        item = {
            "id": "123,1",
            "active": True,
            "live": True,
            "refs": [],
            "jobs": [],
        }
        result = fmt_status_item(item)
        assert "project" not in result

    def test_fmt_status_item_with_dict_refs(self):
        """Normal dict refs should still work."""
        item = {
            "id": "123,1",
            "active": True,
            "live": True,
            "refs": [{"project": "org/repo", "change": 42, "ref": "refs/changes/42/42/1"}],
            "jobs": [],
        }
        result = fmt_status_item(item)
        assert result["project"] == "org/repo"
        assert result["change"] == 42

    def test_fmt_buildset_with_non_dict_refs(self):
        """Non-dict refs in buildset must not crash."""
        bs = {
            "uuid": "bs-1",
            "result": "SUCCESS",
            "refs": [42],  # int, not dict
        }
        result = fmt_buildset(bs)
        assert result["uuid"] == "bs-1"
        # Should not have extracted ref fields
        assert "project" not in result


# -- URL-encoded path traversal --


class TestUrlEncodedPathTraversal:
    @respx.mock
    async def test_percent_encoded_dotdot_in_log_name(self, mock_ctx):
        """log_name with %2e%2e should be rejected."""
        build = make_build()
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/b1").mock(
            return_value=httpx.Response(200, json=build)
        )
        result = json.loads(
            await get_build_log(mock_ctx, "b1", log_name="%2e%2e/%2e%2e/etc/passwd")
        )
        assert "error" in result

    @respx.mock
    async def test_percent_encoded_slash_in_log_name(self, mock_ctx):
        """log_name with %2f-encoded traversal should be rejected."""
        build = make_build()
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/b1").mock(
            return_value=httpx.Response(200, json=build)
        )
        result = json.loads(await get_build_log(mock_ctx, "b1", log_name="foo%2f..%2fbar"))
        assert "error" in result

    @respx.mock
    async def test_percent_encoded_dotdot_in_browse_path(self, mock_ctx):
        """browse_build_logs path with %2e%2e should be rejected."""
        build = make_build()
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/b1").mock(
            return_value=httpx.Response(200, json=build)
        )
        result = json.loads(
            await browse_build_logs(mock_ctx, "b1", path="%2e%2e/%2e%2e/etc/passwd")
        )
        assert "error" in result

    @respx.mock
    async def test_literal_traversal_still_blocked(self, mock_ctx):
        """Original literal .. traversal must still be blocked."""
        build = make_build()
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/b1").mock(
            return_value=httpx.Response(200, json=build)
        )
        result = json.loads(await get_build_log(mock_ctx, "b1", log_name="../../etc/passwd"))
        assert "error" in result

    @respx.mock
    async def test_percent_encoded_dotdot_in_tail_log_name(self, mock_ctx):
        """tail_build_log log_name with %2e%2e should be rejected."""
        build = make_build()
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/b1").mock(
            return_value=httpx.Response(200, json=build)
        )
        result = json.loads(
            await tail_build_log(mock_ctx, "b1", log_name="%2e%2e/%2e%2e/etc/passwd")
        )
        assert "error" in result

    @respx.mock
    async def test_literal_traversal_blocked_in_tail(self, mock_ctx):
        """tail_build_log must also block literal .. traversal."""
        build = make_build()
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/b1").mock(
            return_value=httpx.Response(200, json=build)
        )
        result = json.loads(await tail_build_log(mock_ctx, "b1", log_name="../../etc/passwd"))
        assert "error" in result

    @respx.mock
    async def test_valid_log_name_not_blocked(self, mock_ctx):
        """Normal log names like 'job-output.txt' must not be blocked."""
        build = make_build()
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/b1").mock(
            return_value=httpx.Response(200, json=build)
        )
        log_url = build["log_url"] + "job-output.txt"
        respx.get(log_url).mock(return_value=httpx.Response(200, content=b"ok\n"))
        result = json.loads(await get_build_log(mock_ctx, "b1", log_name="job-output.txt"))
        assert "error" not in result


# -- ReDoS pre-validation --


class TestRedosPreValidation:
    @respx.mock
    async def test_nested_plus_plus_rejected(self, mock_ctx):
        """Pattern (a+)+ should be rejected before compilation."""
        build = make_build()
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/b1").mock(
            return_value=httpx.Response(200, json=build)
        )
        log_url = build["log_url"] + "job-output.txt"
        respx.get(log_url).mock(return_value=httpx.Response(200, content=b"test\n"))
        result = json.loads(await get_build_log(mock_ctx, "b1", grep="(a+)+b"))
        assert "error" in result
        assert (
            "nested quantifier" in result["error"].lower()
            or "backtracking" in result["error"].lower()
        )

    @respx.mock
    async def test_nested_star_plus_rejected(self, mock_ctx):
        """Pattern (a*)+b should be rejected."""
        build = make_build()
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/b1").mock(
            return_value=httpx.Response(200, json=build)
        )
        log_url = build["log_url"] + "job-output.txt"
        respx.get(log_url).mock(return_value=httpx.Response(200, content=b"test\n"))
        result = json.loads(await get_build_log(mock_ctx, "b1", grep="(a*)+b"))
        assert "error" in result

    @respx.mock
    async def test_simple_alternation_allowed(self, mock_ctx):
        """Pattern error|failed should be allowed (no nested quantifiers)."""
        build = make_build()
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/b1").mock(
            return_value=httpx.Response(200, json=build)
        )
        log_url = build["log_url"] + "job-output.txt"
        respx.get(log_url).mock(return_value=httpx.Response(200, content=b"an error here\n"))
        result = json.loads(await get_build_log(mock_ctx, "b1", grep="error|failed"))
        assert "error" not in result

    @respx.mock
    async def test_simple_quantifier_allowed(self, mock_ctx):
        """Pattern like a+ (simple quantifier, not nested) should be allowed."""
        build = make_build()
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/b1").mock(
            return_value=httpx.Response(200, json=build)
        )
        log_url = build["log_url"] + "job-output.txt"
        respx.get(log_url).mock(return_value=httpx.Response(200, content=b"aaaa\n"))
        result = json.loads(await get_build_log(mock_ctx, "b1", grep="a+"))
        assert "error" not in result


# -- smart_truncate size accuracy --


class TestSmartTruncateSize:
    def test_output_within_max_size(self):
        """Truncated output must not exceed max_size."""
        text = "x" * 10000
        for max_size in [100, 200, 500, 1000, 4000]:
            result = smart_truncate(text, max_size)
            assert result is not None
            assert len(result) <= max_size, f"max_size={max_size}, actual={len(result)}"

    def test_short_text_returned_as_is(self):
        result = smart_truncate("short text", 100)
        assert result == "short text"

    def test_empty_text_returns_none(self):
        assert smart_truncate("") is None
        assert smart_truncate("", 100) is None

    def test_separator_present_in_truncated_output(self):
        text = "x" * 10000
        result = smart_truncate(text, 200)
        assert result is not None
        assert "[..." in result
        assert "chars omitted" in result


class TestExtractErrors:
    """Unit tests for extract_errors()."""

    def test_returns_none_for_short_text(self):
        from mcp_zuul.parsers import extract_errors

        assert extract_errors("short text with fatal: error") is None

    def test_extracts_fatal_pattern(self):
        from mcp_zuul.parsers import extract_errors

        filler = "normal line\n" * 300
        text = filler + 'fatal: [host]: FAILED! => {"msg": "timeout"}\n' + filler
        result = extract_errors(text)
        assert result is not None
        assert len(result) == 1
        assert "timeout" in result[0]

    def test_extracts_level_error_pattern(self):
        from mcp_zuul.parsers import extract_errors

        filler = "normal\n" * 300
        text = filler + "level=error msg=bootstrap failed\n" + filler
        result = extract_errors(text)
        assert result is not None
        assert any("bootstrap failed" in e for e in result)

    def test_caps_at_max_errors(self):
        from mcp_zuul.parsers import extract_errors

        filler = "normal output line\n" * 300
        errors = "".join(
            f'fatal: [host-{i}]: FAILED! => {{"msg": "error {i}"}}\n' for i in range(20)
        )
        text = filler + errors + filler
        result = extract_errors(text, max_errors=3)
        assert result is not None
        assert len(result) == 3

    def test_deduplicates_identical_matches(self):
        from mcp_zuul.parsers import extract_errors

        filler = "normal output line with content\n" * 300
        # Same error appearing twice at different positions
        err = 'fatal: [host]: FAILED! => {"msg": "same error"}\n'
        text = filler + err + filler + err + filler
        result = extract_errors(text)
        assert result is not None
        assert len(result) == 1

    def test_returns_none_when_no_patterns_match(self):
        from mcp_zuul.parsers import extract_errors

        text = "normal output\n" * 500
        assert extract_errors(text) is None

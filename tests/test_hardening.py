"""Tests for security and correctness hardening changes."""

import json

import httpx
import pytest
import respx

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
from mcp_zuul.server import _BearerAuth
from mcp_zuul.tools import list_jobs, list_projects

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

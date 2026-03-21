"""Integration tests for build and buildset tools."""

import json

import httpx
import respx

from mcp_zuul.tools import (
    get_build,
    get_build_failures,
    get_buildset,
    list_builds,
    list_buildsets,
)
from tests.conftest import make_build, make_buildset, make_job_output_json


class TestListBuilds:
    @respx.mock
    async def test_returns_builds_with_pagination(self, mock_ctx):
        builds = [make_build(uuid=f"uuid-{i}") for i in range(3)]
        respx.get("https://zuul.example.com/api/tenant/test-tenant/builds").mock(
            return_value=httpx.Response(200, json=builds)
        )
        result = json.loads(await list_builds(mock_ctx, limit=5))
        assert result["count"] == 3
        assert result["has_more"] is False

    @respx.mock
    async def test_has_more_when_exceeds_limit(self, mock_ctx):
        builds = [make_build(uuid=f"uuid-{i}") for i in range(3)]
        respx.get("https://zuul.example.com/api/tenant/test-tenant/builds").mock(
            return_value=httpx.Response(200, json=builds)
        )
        result = json.loads(await list_builds(mock_ctx, limit=2))
        assert result["count"] == 2
        assert result["has_more"] is True

    @respx.mock
    async def test_filters_passed_as_params(self, mock_ctx):
        route = respx.get("https://zuul.example.com/api/tenant/test-tenant/builds").mock(
            return_value=httpx.Response(200, json=[])
        )
        await list_builds(mock_ctx, project="org/repo", result="FAILURE", job_name="test-job")
        assert route.called
        params = dict(route.calls[0].request.url.params)
        assert params["project"] == "org/repo"
        assert params["result"] == "FAILURE"
        assert params["job_name"] == "test-job"

    @respx.mock
    async def test_limit_clamped(self, mock_ctx):
        route = respx.get("https://zuul.example.com/api/tenant/test-tenant/builds").mock(
            return_value=httpx.Response(200, json=[])
        )
        await list_builds(mock_ctx, limit=500)
        params = dict(route.calls[0].request.url.params)
        assert params["limit"] == "101"  # clamped to 100 + 1


class TestGetBuild:
    @respx.mock
    async def test_returns_full_build(self, mock_ctx):
        build = make_build()
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/build-uuid-1").mock(
            return_value=httpx.Response(200, json=build)
        )
        result = json.loads(await get_build(mock_ctx, "build-uuid-1"))
        assert result["uuid"] == "build-uuid-1"
        assert result["job"] == "test-job"
        assert "log_url" in result  # brief=False includes log_url
        assert "nodeset" in result


class TestGetBuildUrl:
    @respx.mock
    async def test_accepts_zuul_url(self, mock_ctx):
        build = make_build(uuid="url-build-uuid")
        respx.get("https://zuul.example.com/api/tenant/my-tenant/build/url-build-uuid").mock(
            return_value=httpx.Response(200, json=build)
        )
        result = json.loads(
            await get_build(
                mock_ctx,
                url="https://zuul.example.com/t/my-tenant/build/url-build-uuid",
            )
        )
        assert result["uuid"] == "url-build-uuid"

    @respx.mock
    async def test_url_with_zuul_prefix(self, mock_ctx):
        build = make_build(uuid="abc123")
        respx.get("https://zuul.example.com/api/tenant/comp-int/build/abc123").mock(
            return_value=httpx.Response(200, json=build)
        )
        result = json.loads(
            await get_build(
                mock_ctx,
                url="https://sf.example.com/zuul/t/comp-int/build/abc123",
            )
        )
        assert result["uuid"] == "abc123"

    async def test_invalid_url_returns_error(self, mock_ctx):
        result = json.loads(await get_build(mock_ctx, url="https://example.com/not-a-zuul-url"))
        assert "error" in result
        assert "Cannot parse" in result["error"]

    async def test_wrong_url_type_returns_error(self, mock_ctx):
        result = json.loads(
            await get_build(
                mock_ctx,
                url="https://zuul.example.com/t/tenant/buildset/some-uuid",
            )
        )
        assert "error" in result
        assert "Expected build" in result["error"]

    async def test_no_uuid_no_url_returns_error(self, mock_ctx):
        result = json.loads(await get_build(mock_ctx))
        assert "error" in result

    @respx.mock
    async def test_explicit_tenant_overrides_url_tenant(self, mock_ctx):
        build = make_build(uuid="override-uuid")
        respx.get("https://zuul.example.com/api/tenant/explicit/build/override-uuid").mock(
            return_value=httpx.Response(200, json=build)
        )
        result = json.loads(
            await get_build(
                mock_ctx,
                url="https://zuul.example.com/t/url-tenant/build/override-uuid",
                tenant="explicit",
            )
        )
        assert result["uuid"] == "override-uuid"


class TestGetBuildFailures:
    @respx.mock
    async def test_parses_failed_tasks(self, mock_ctx):
        build = make_build(result="FAILURE")
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/fail-uuid").mock(
            return_value=httpx.Response(200, json=build)
        )
        respx.get(f"{build['log_url']}job-output.json.gz").mock(
            return_value=httpx.Response(200, json=make_job_output_json(failed=True))
        )
        result = json.loads(await get_build_failures(mock_ctx, "fail-uuid"))
        assert result["result"] == "FAILURE"
        assert len(result["failed_tasks"]) == 1
        assert result["failed_tasks"][0]["task"] == "Run deployment"
        assert result["failed_tasks"][0]["host"] == "controller-0"
        assert result["failed_tasks"][0]["rc"] == 1
        # Only failed playbooks included (not passing ones)
        assert len(result["failed_playbooks"]) == 1
        assert result["total_playbooks"] == 1

    @respx.mock
    async def test_no_log_url(self, mock_ctx):
        build = make_build(log_url=None)
        build["log_url"] = None
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/no-log").mock(
            return_value=httpx.Response(200, json=build)
        )
        result = json.loads(await get_build_failures(mock_ctx, "no-log"))
        assert "error" in result

    @respx.mock
    async def test_fallback_to_uncompressed(self, mock_ctx):
        build = make_build()
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/build-uuid-1").mock(
            return_value=httpx.Response(200, json=build)
        )
        respx.get(f"{build['log_url']}job-output.json.gz").mock(return_value=httpx.Response(404))
        respx.get(f"{build['log_url']}job-output.json").mock(
            return_value=httpx.Response(200, json=make_job_output_json(failed=False))
        )
        result = json.loads(await get_build_failures(mock_ctx, "build-uuid-1"))
        assert result["result"] == "SUCCESS"
        assert len(result["failed_tasks"]) == 0

    @respx.mock
    async def test_json_not_found(self, mock_ctx):
        build = make_build()
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/build-uuid-1").mock(
            return_value=httpx.Response(200, json=build)
        )
        respx.get(f"{build['log_url']}job-output.json.gz").mock(return_value=httpx.Response(404))
        respx.get(f"{build['log_url']}job-output.json").mock(return_value=httpx.Response(404))
        result = json.loads(await get_build_failures(mock_ctx, "build-uuid-1"))
        assert "error" in result
        assert "not found" in result["error"]


class TestListBuildsets:
    @respx.mock
    async def test_returns_buildsets(self, mock_ctx):
        buildsets = [make_buildset(uuid=f"bs-{i}") for i in range(2)]
        respx.get("https://zuul.example.com/api/tenant/test-tenant/buildsets").mock(
            return_value=httpx.Response(200, json=buildsets)
        )
        result = json.loads(await list_buildsets(mock_ctx))
        assert result["count"] == 2
        assert result["buildsets"][0]["uuid"] == "bs-0"

    @respx.mock
    async def test_include_builds_fetches_details(self, mock_ctx):
        buildsets = [make_buildset(uuid="bs-1")]
        respx.get("https://zuul.example.com/api/tenant/test-tenant/buildsets").mock(
            return_value=httpx.Response(200, json=buildsets)
        )
        respx.get("https://zuul.example.com/api/tenant/test-tenant/buildset/bs-1").mock(
            return_value=httpx.Response(200, json=make_buildset(uuid="bs-1"))
        )
        result = json.loads(await list_buildsets(mock_ctx, include_builds=True))
        assert "builds" in result["buildsets"][0]


class TestGetBuildset:
    @respx.mock
    async def test_returns_full_buildset(self, mock_ctx):
        respx.get("https://zuul.example.com/api/tenant/test-tenant/buildset/bs-uuid").mock(
            return_value=httpx.Response(200, json=make_buildset())
        )
        result = json.loads(await get_buildset(mock_ctx, "bs-uuid"))
        assert result["uuid"] == "buildset-uuid-1"
        assert "builds" in result
        assert "events" in result

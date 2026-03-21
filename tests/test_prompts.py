"""Tests for MCP prompt templates."""

import httpx
import respx

from mcp_zuul.prompts import debug_build
from tests.conftest import make_build, make_job_output_json


class TestDebugBuild:
    @respx.mock
    async def test_includes_build_details(self, mock_ctx):
        build = make_build(uuid="fail-uuid", result="FAILURE")
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/fail-uuid").mock(
            return_value=httpx.Response(200, json=build)
        )
        respx.get(f"{build['log_url']}job-output.json.gz").mock(return_value=httpx.Response(404))
        respx.get(f"{build['log_url']}job-output.json").mock(return_value=httpx.Response(404))
        result = await debug_build(uuid="fail-uuid", ctx=mock_ctx)
        assert "Build Details" in result
        assert "fail-uuid" in result
        assert "FAILURE" in result

    @respx.mock
    async def test_includes_failed_tasks(self, mock_ctx):
        build = make_build(uuid="fail-uuid", result="FAILURE")
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/fail-uuid").mock(
            return_value=httpx.Response(200, json=build)
        )
        respx.get(f"{build['log_url']}job-output.json.gz").mock(
            return_value=httpx.Response(200, json=make_job_output_json(failed=True))
        )
        result = await debug_build(uuid="fail-uuid", ctx=mock_ctx)
        assert "Failed Tasks" in result
        assert "Run deployment" in result
        assert "controller-0" in result

    @respx.mock
    async def test_skips_failures_for_success(self, mock_ctx):
        build = make_build(uuid="ok-uuid", result="SUCCESS")
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/ok-uuid").mock(
            return_value=httpx.Response(200, json=build)
        )
        result = await debug_build(uuid="ok-uuid", ctx=mock_ctx)
        assert "Build Details" in result
        assert "Failed Tasks" not in result

    @respx.mock
    async def test_includes_next_steps(self, mock_ctx):
        build = make_build(uuid="uuid-1", result="FAILURE", log_url=None)
        build["log_url"] = None
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/uuid-1").mock(
            return_value=httpx.Response(200, json=build)
        )
        result = await debug_build(uuid="uuid-1", ctx=mock_ctx)
        assert "get_build_log" in result
        assert "Next Steps" in result

    @respx.mock
    async def test_custom_tenant(self, mock_ctx):
        build = make_build(uuid="t-uuid", result="FAILURE")
        respx.get("https://zuul.example.com/api/tenant/custom/build/t-uuid").mock(
            return_value=httpx.Response(200, json=build)
        )
        respx.get(f"{build['log_url']}job-output.json.gz").mock(return_value=httpx.Response(404))
        respx.get(f"{build['log_url']}job-output.json").mock(return_value=httpx.Response(404))
        result = await debug_build(uuid="t-uuid", tenant="custom", ctx=mock_ctx)
        assert 'tenant="custom"' in result

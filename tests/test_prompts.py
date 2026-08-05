"""Tests for MCP prompt templates."""

import httpx
import respx

from mcp_zuul.prompts import (
    check_change,
    compare_builds,
    debug_build,
    diagnose_queue_delay,
    tenant_health,
)
from tests.conftest import make_build, make_buildset, make_job_output_json, make_status_item


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


class TestCompareBuilds:
    @respx.mock
    async def test_includes_both_builds(self, mock_ctx):
        b1 = make_build(uuid="b1", result="SUCCESS")
        b2 = make_build(uuid="b2", result="FAILURE")
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/b1").mock(
            return_value=httpx.Response(200, json=b1)
        )
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/b2").mock(
            return_value=httpx.Response(200, json=b2)
        )
        # Mock job-output fetch for the failed build (via shared _fetch_job_output)
        respx.get(f"{b2['log_url']}job-output.json.gz").mock(return_value=httpx.Response(404))
        respx.get(f"{b2['log_url']}job-output.json").mock(return_value=httpx.Response(404))
        result = await compare_builds(uuid1="b1", uuid2="b2", ctx=mock_ctx)
        assert "Build A" in result
        assert "Build B" in result
        assert "SUCCESS" in result
        assert "FAILURE" in result

    @respx.mock
    async def test_includes_failure_data(self, mock_ctx):
        b1 = make_build(uuid="b1", result="SUCCESS")
        b2 = make_build(uuid="b2", result="FAILURE")
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/b1").mock(
            return_value=httpx.Response(200, json=b1)
        )
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/b2").mock(
            return_value=httpx.Response(200, json=b2)
        )
        respx.get(f"{b2['log_url']}job-output.json.gz").mock(
            return_value=httpx.Response(200, json=make_job_output_json(failed=True))
        )
        result = await compare_builds(uuid1="b1", uuid2="b2", ctx=mock_ctx)
        assert "Build B Failures" in result
        assert "Run deployment" in result


class TestCheckChange:
    @respx.mock
    async def test_live_pipeline(self, mock_ctx):
        item = make_status_item(change=12345)
        respx.get("https://zuul.example.com/api/tenant/test-tenant/status/change/12345").mock(
            return_value=httpx.Response(200, json=[item])
        )
        result = await check_change(change="12345", ctx=mock_ctx)
        assert "Live pipeline status" in result
        assert "12345" in result

    @respx.mock
    async def test_not_in_pipeline_with_buildset(self, mock_ctx):
        bs = make_buildset(uuid="bs-1")
        respx.get("https://zuul.example.com/api/tenant/test-tenant/status/change/99999").mock(
            return_value=httpx.Response(200, json=[])
        )
        respx.get("https://zuul.example.com/api/tenant/test-tenant/buildsets").mock(
            return_value=httpx.Response(200, json=[{"uuid": "bs-1"}])
        )
        respx.get("https://zuul.example.com/api/tenant/test-tenant/buildset/bs-1").mock(
            return_value=httpx.Response(200, json=bs)
        )
        result = await check_change(change="99999", ctx=mock_ctx)
        assert "not currently in any pipeline" in result
        assert "Latest buildset" in result

    @respx.mock
    async def test_no_history(self, mock_ctx):
        respx.get("https://zuul.example.com/api/tenant/test-tenant/status/change/00000").mock(
            return_value=httpx.Response(200, json=[])
        )
        respx.get("https://zuul.example.com/api/tenant/test-tenant/buildsets").mock(
            return_value=httpx.Response(200, json=[])
        )
        result = await check_change(change="00000", ctx=mock_ctx)
        assert "no build history" in result
        assert "get_config_errors" in result


class TestTenantHealth:
    @respx.mock
    async def test_includes_all_sections(self, mock_ctx):
        respx.get("https://zuul.example.com/api/components").mock(
            return_value=httpx.Response(
                200,
                json={
                    "schedulers": [{"hostname": "s1", "state": "running", "version": "10"}],
                    "executors": [{"hostname": "e1", "state": "running", "version": "10"}],
                },
            )
        )
        respx.get("https://zuul.example.com/api/tenant/test-tenant/config-errors").mock(
            return_value=httpx.Response(200, json=[])
        )
        respx.get("https://zuul.example.com/api/tenant/test-tenant/nodes").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {"state": "ready"},
                    {"state": "in-use"},
                    {"state": "building"},
                ],
            )
        )
        result = await tenant_health(ctx=mock_ctx)
        assert "System Components" in result
        assert "Config Errors (0)" in result
        assert "Node Pool" in result
        assert "Analysis" in result
        assert '"total": 3' in result

    @respx.mock
    async def test_config_errors_counted(self, mock_ctx):
        respx.get("https://zuul.example.com/api/components").mock(
            return_value=httpx.Response(
                200,
                json={
                    "schedulers": [{"hostname": "s1", "state": "paused", "version": "10"}],
                    "executors": [],
                },
            )
        )
        respx.get("https://zuul.example.com/api/tenant/test-tenant/config-errors").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {"error": "syntax error", "source_context": {}},
                ],
            )
        )
        respx.get("https://zuul.example.com/api/tenant/test-tenant/nodes").mock(
            return_value=httpx.Response(200, json=[])
        )
        result = await tenant_health(ctx=mock_ctx)
        assert "Config Errors (1)" in result
        assert "paused" in result

    @respx.mock
    async def test_custom_tenant(self, mock_ctx):
        respx.get("https://zuul.example.com/api/components").mock(
            return_value=httpx.Response(200, json={"schedulers": [], "executors": []})
        )
        respx.get("https://zuul.example.com/api/tenant/custom/config-errors").mock(
            return_value=httpx.Response(200, json=[])
        )
        respx.get("https://zuul.example.com/api/tenant/custom/nodes").mock(
            return_value=httpx.Response(200, json=[])
        )
        result = await tenant_health(tenant="custom", ctx=mock_ctx)
        assert '"custom"' in result

    @respx.mock
    async def test_partial_api_failure_still_renders(self, mock_ctx):
        """If one API fails, prompt still renders with available data."""
        respx.get("https://zuul.example.com/api/components").mock(
            return_value=httpx.Response(500, text="Internal Server Error")
        )
        respx.get("https://zuul.example.com/api/tenant/test-tenant/config-errors").mock(
            return_value=httpx.Response(200, json=[{"error": "bad yaml", "source_context": {}}])
        )
        respx.get("https://zuul.example.com/api/tenant/test-tenant/nodes").mock(
            return_value=httpx.Response(200, json=[{"state": "ready"}])
        )
        result = await tenant_health(ctx=mock_ctx)
        assert "Config Errors (1)" in result
        assert "Node Pool" in result
        assert '"total": 1' in result


class TestDiagnoseQueueDelay:
    @respx.mock
    async def test_includes_all_sections(self, mock_ctx):
        from tests.conftest import make_status_pipeline

        respx.get("https://zuul.example.com/api/tenant/test-tenant/status").mock(
            return_value=httpx.Response(
                200,
                json={
                    "zuul_version": "10.0.0",
                    "pipelines": [make_status_pipeline("check")],
                },
            )
        )
        respx.get("https://zuul.example.com/api/tenant/test-tenant/nodes").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {"state": "in-use"},
                    {"state": "in-use"},
                ],
            )
        )
        respx.get("https://zuul.example.com/api/tenant/test-tenant/semaphores").mock(
            return_value=httpx.Response(200, json=[])
        )
        respx.get("https://zuul.example.com/api/components").mock(
            return_value=httpx.Response(
                200,
                json={
                    "schedulers": [{"hostname": "s1", "state": "running", "version": "10"}],
                    "executors": [{"hostname": "e1", "state": "running", "version": "10"}],
                },
            )
        )
        result = await diagnose_queue_delay(ctx=mock_ctx)
        assert "Pipeline Status" in result
        assert "Node Pool" in result
        assert "Semaphores" in result
        assert "System Components" in result
        assert "Analysis" in result

    @respx.mock
    async def test_active_semaphores_shown(self, mock_ctx):
        from tests.conftest import make_status_pipeline

        respx.get("https://zuul.example.com/api/tenant/test-tenant/status").mock(
            return_value=httpx.Response(
                200,
                json={
                    "zuul_version": "10.0.0",
                    "pipelines": [make_status_pipeline("gate")],
                },
            )
        )
        respx.get("https://zuul.example.com/api/tenant/test-tenant/nodes").mock(
            return_value=httpx.Response(200, json=[{"state": "ready"}])
        )
        respx.get("https://zuul.example.com/api/tenant/test-tenant/semaphores").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {"name": "deploy-lock", "global": False, "max": 1, "holders": {"count": 1}},
                    {"name": "idle-sem", "global": False, "max": 5, "holders": {"count": 0}},
                ],
            )
        )
        respx.get("https://zuul.example.com/api/components").mock(
            return_value=httpx.Response(
                200,
                json={
                    "schedulers": [{"hostname": "s1", "state": "running", "version": "10"}],
                    "executors": [{"hostname": "e1", "state": "running", "version": "10"}],
                },
            )
        )
        result = await diagnose_queue_delay(ctx=mock_ctx)
        assert "deploy-lock" in result
        assert "idle-sem" not in result

    @respx.mock
    async def test_pipeline_filter(self, mock_ctx):
        from tests.conftest import make_status_pipeline

        respx.get("https://zuul.example.com/api/tenant/test-tenant/status").mock(
            return_value=httpx.Response(
                200,
                json={
                    "zuul_version": "10.0.0",
                    "pipelines": [
                        make_status_pipeline("check"),
                        make_status_pipeline("gate"),
                    ],
                },
            )
        )
        respx.get("https://zuul.example.com/api/tenant/test-tenant/nodes").mock(
            return_value=httpx.Response(200, json=[])
        )
        respx.get("https://zuul.example.com/api/tenant/test-tenant/semaphores").mock(
            return_value=httpx.Response(200, json=[])
        )
        respx.get("https://zuul.example.com/api/components").mock(
            return_value=httpx.Response(
                200,
                json={
                    "schedulers": [{"hostname": "s1", "state": "running", "version": "10"}],
                    "executors": [{"hostname": "e1", "state": "running", "version": "10"}],
                },
            )
        )
        result = await diagnose_queue_delay(pipeline="gate", ctx=mock_ctx)
        assert "gate" in result

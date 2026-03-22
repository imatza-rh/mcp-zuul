"""Integration tests for status tools."""

import json

import httpx
import respx

from mcp_zuul.tools import get_change_status, get_status
from tests.conftest import make_buildset, make_status_item, make_status_pipeline


class TestGetStatus:
    @respx.mock
    async def test_returns_active_pipelines(self, mock_ctx):
        respx.get("https://zuul.example.com/api/tenant/test-tenant/status").mock(
            return_value=httpx.Response(
                200,
                json={
                    "zuul_version": "10.0.0",
                    "pipelines": [
                        make_status_pipeline("check"),
                        make_status_pipeline("gate", items=[]),
                    ],
                },
            )
        )
        result = json.loads(await get_status(mock_ctx))
        assert result["zuul_version"] == "10.0.0"
        # gate has no items, should be filtered with active_only=True
        assert result["pipeline_count"] == 1
        assert result["pipelines"][0]["pipeline"] == "check"

    @respx.mock
    async def test_filter_by_pipeline(self, mock_ctx):
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
        result = json.loads(await get_status(mock_ctx, pipeline="gate"))
        assert result["pipeline_count"] == 1
        assert result["pipelines"][0]["pipeline"] == "gate"

    @respx.mock
    async def test_filter_by_project(self, mock_ctx):
        item1 = make_status_item(change=111)
        item1["refs"][0]["project"] = "org/repo-a"
        item2 = make_status_item(change=222)
        item2["refs"][0]["project"] = "org/repo-b"
        respx.get("https://zuul.example.com/api/tenant/test-tenant/status").mock(
            return_value=httpx.Response(
                200,
                json={
                    "zuul_version": "10.0.0",
                    "pipelines": [
                        {"name": "check", "change_queues": [{"heads": [[item1, item2]]}]}
                    ],
                },
            )
        )
        result = json.loads(await get_status(mock_ctx, project="repo-a"))
        items = result["pipelines"][0]["items"]
        assert len(items) == 1
        assert items[0]["project"] == "org/repo-a"


class TestGetChangeStatus:
    @respx.mock
    async def test_change_in_pipeline(self, mock_ctx):
        item = make_status_item(change=12345)
        respx.get("https://zuul.example.com/api/tenant/test-tenant/status/change/12345").mock(
            return_value=httpx.Response(200, json=[item])
        )
        result = json.loads(await get_change_status(mock_ctx, "12345"))
        assert isinstance(result, list)
        assert result[0]["project"] == "org/repo"
        assert "jobs" in result[0]
        assert "status_url" in result[0]

    @respx.mock
    async def test_change_not_in_pipeline_fetches_latest_buildset(self, mock_ctx):
        respx.get("https://zuul.example.com/api/tenant/test-tenant/status/change/99999").mock(
            return_value=httpx.Response(200, json=[])
        )
        # Bare digit triggers full status fallback scan
        respx.get("https://zuul.example.com/api/tenant/test-tenant/status").mock(
            return_value=httpx.Response(200, json={"zuul_version": "10", "pipelines": []})
        )
        respx.get("https://zuul.example.com/api/tenant/test-tenant/buildsets").mock(
            return_value=httpx.Response(200, json=[{"uuid": "bs-latest"}])
        )
        respx.get("https://zuul.example.com/api/tenant/test-tenant/buildset/bs-latest").mock(
            return_value=httpx.Response(200, json=make_buildset(uuid="bs-latest"))
        )
        result = json.loads(await get_change_status(mock_ctx, "99999"))
        assert result["status"] == "not_in_pipeline"
        assert result["latest_buildset"]["uuid"] == "bs-latest"

    @respx.mock
    async def test_change_not_in_pipeline_no_buildsets(self, mock_ctx):
        respx.get("https://zuul.example.com/api/tenant/test-tenant/status/change/88888").mock(
            return_value=httpx.Response(200, json=[])
        )
        # Bare digit triggers full status fallback scan
        respx.get("https://zuul.example.com/api/tenant/test-tenant/status").mock(
            return_value=httpx.Response(200, json={"zuul_version": "10", "pipelines": []})
        )
        respx.get("https://zuul.example.com/api/tenant/test-tenant/buildsets").mock(
            return_value=httpx.Response(200, json=[])
        )
        result = json.loads(await get_change_status(mock_ctx, "88888"))
        assert result["status"] == "not_in_pipeline"
        assert "latest_buildset" not in result

    @respx.mock
    async def test_gitlab_ref_fallback(self, mock_ctx):
        """Bare change number with no direct match falls back to full status scan."""
        respx.get("https://zuul.example.com/api/tenant/test-tenant/status/change/1925").mock(
            return_value=httpx.Response(200, json=[])
        )
        item = make_status_item(change=1925)
        item["refs"][0]["ref"] = "refs/merge-requests/1925/head"
        respx.get("https://zuul.example.com/api/tenant/test-tenant/status").mock(
            return_value=httpx.Response(
                200,
                json={
                    "zuul_version": "10.0.0",
                    "pipelines": [{"name": "check", "change_queues": [{"heads": [[item]]}]}],
                },
            )
        )
        result = json.loads(await get_change_status(mock_ctx, "1925"))
        assert isinstance(result, list)
        assert len(result) == 1

    @respx.mock
    async def test_pre_fail_preserved_in_output(self, mock_ctx):
        """Verify pre_fail=True is included in formatted job output."""
        item = make_status_item(change=77777)
        item["jobs"][0]["pre_fail"] = True
        respx.get("https://zuul.example.com/api/tenant/test-tenant/status/change/77777").mock(
            return_value=httpx.Response(200, json=[item])
        )
        result = json.loads(await get_change_status(mock_ctx, "77777"))
        assert result[0]["jobs"][0]["pre_fail"] is True

    @respx.mock
    async def test_failing_reasons_with_pre_fail(self, mock_ctx):
        """Verify failing_reasons are preserved alongside pre_fail."""
        item = make_status_item(change=66666)
        item["failing_reasons"] = ["test-job"]
        item["jobs"][0]["pre_fail"] = True
        respx.get("https://zuul.example.com/api/tenant/test-tenant/status/change/66666").mock(
            return_value=httpx.Response(200, json=[item])
        )
        result = json.loads(await get_change_status(mock_ctx, "66666"))
        assert result[0]["failing_reasons"] == ["test-job"]
        assert result[0]["jobs"][0]["pre_fail"] is True

    @respx.mock
    async def test_tenant_required_error(self, mock_ctx):
        mock_ctx.request_context.lifespan_context.config.default_tenant = ""
        result = json.loads(await get_change_status(mock_ctx, "12345"))
        assert "error" in result
        assert "tenant" in result["error"].lower()

    @respx.mock
    async def test_accepts_change_url(self, mock_ctx):
        item = make_status_item(change=99999)
        respx.get("https://zuul.example.com/api/tenant/my-tenant/status/change/99999%2Cabc").mock(
            return_value=httpx.Response(200, json=[item])
        )
        result = json.loads(
            await get_change_status(
                mock_ctx,
                url="https://zuul.example.com/t/my-tenant/status/change/99999,abc",
            )
        )
        assert isinstance(result, list)
        assert len(result) == 1

    async def test_wrong_url_type_for_change(self, mock_ctx):
        result = json.loads(
            await get_change_status(
                mock_ctx,
                url="https://zuul.example.com/t/tenant/build/some-uuid",
            )
        )
        assert "error" in result
        assert "Expected change" in result["error"]

    @respx.mock
    async def test_github_ref_extracts_change_number(self, mock_ctx):
        """refs/pull/123/head should be normalized to change number 123."""
        item = make_status_item(change=123)
        respx.get("https://zuul.example.com/api/tenant/test-tenant/status/change/123").mock(
            return_value=httpx.Response(200, json=[item])
        )
        result = json.loads(await get_change_status(mock_ctx, change="refs/pull/123/head"))
        assert isinstance(result, list)
        assert len(result) == 1

    @respx.mock
    async def test_gitlab_mr_ref_extracts_change_number(self, mock_ctx):
        """refs/merge-requests/456/head should be normalized to change number 456."""
        item = make_status_item(change=456)
        respx.get("https://zuul.example.com/api/tenant/test-tenant/status/change/456").mock(
            return_value=httpx.Response(200, json=[item])
        )
        result = json.loads(
            await get_change_status(mock_ctx, change="refs/merge-requests/456/head")
        )
        assert isinstance(result, list)
        assert len(result) == 1

    @respx.mock
    async def test_elapsed_computed_from_start_time_for_running_jobs(self, mock_ctx):
        """Zuul's elapsed_time can be stale. Verify we recompute from start_time."""
        import time

        now = time.time()
        # Job started 600 seconds (10 min) ago, but Zuul reports stale 60s elapsed
        item = make_status_item(change=55555)
        item["jobs"][0]["start_time"] = now - 600
        item["jobs"][0]["elapsed_time"] = 60000  # Stale: 60s
        item["jobs"][0]["result"] = None  # Still running
        respx.get("https://zuul.example.com/api/tenant/test-tenant/status/change/55555").mock(
            return_value=httpx.Response(200, json=[item])
        )
        result = json.loads(await get_change_status(mock_ctx, "55555"))
        elapsed_ms = result[0]["jobs"][0]["elapsed"]
        # Should be ~600000ms (10 min), not the stale 60000ms
        assert elapsed_ms > 500000, f"Expected ~600000ms, got {elapsed_ms} (stale value used)"

    @respx.mock
    async def test_elapsed_preserved_for_completed_jobs(self, mock_ctx):
        """For completed jobs (with result), keep Zuul's elapsed value."""
        item = make_status_item(change=44444)
        item["jobs"][0]["start_time"] = 1704067200
        item["jobs"][0]["elapsed_time"] = 300000  # 5 min — Zuul's final value
        item["jobs"][0]["result"] = "SUCCESS"
        respx.get("https://zuul.example.com/api/tenant/test-tenant/status/change/44444").mock(
            return_value=httpx.Response(200, json=[item])
        )
        result = json.loads(await get_change_status(mock_ctx, "44444"))
        elapsed_ms = result[0]["jobs"][0]["elapsed"]
        assert elapsed_ms == 300000, "Completed job should keep Zuul's elapsed value"

    async def test_no_change_no_url_returns_error(self, mock_ctx):
        result = json.loads(await get_change_status(mock_ctx))
        assert "error" in result

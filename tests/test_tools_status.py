"""Integration tests for status tools."""

import json
import time

import httpx
import respx

from mcp_zuul.formatters import _compute_chain_summary, _format_duration
from mcp_zuul.tools import get_change_status, get_status
from tests.conftest import (
    make_buildset,
    make_chained_status_item,
    make_status_item,
    make_status_pipeline,
)


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

    @respx.mock
    async def test_status_capped_at_max_items(self, mock_ctx):
        """Responses with more than _MAX_STATUS_ITEMS should be capped."""
        # Per-pipeline cap is 50, so we need 5+ pipelines with 45 items each
        # to exceed the global _MAX_STATUS_ITEMS=200 cap.
        pipelines = []
        for p_idx in range(6):
            items = [make_status_item(change=p_idx * 1000 + i) for i in range(45)]
            pipelines.append({"name": f"pipeline-{p_idx}", "change_queues": [{"heads": [items]}]})
        respx.get("https://zuul.example.com/api/tenant/test-tenant/status").mock(
            return_value=httpx.Response(
                200,
                json={"zuul_version": "10.0.0", "pipelines": pipelines},
            )
        )
        result = json.loads(await get_status(mock_ctx, active_only=False))
        total = sum(p["item_count"] for p in result["pipelines"])
        assert total <= 200
        assert result["capped"] is True
        assert result["cap_limit"] == 200

    @respx.mock
    async def test_per_pipeline_cap_indicator(self, mock_ctx):
        """Pipelines exceeding 50 items should show pipeline_capped=True."""
        items = [make_status_item(change=i) for i in range(60)]
        pipelines = [{"name": "check", "change_queues": [{"heads": [items]}]}]
        respx.get("https://zuul.example.com/api/tenant/test-tenant/status").mock(
            return_value=httpx.Response(
                200,
                json={"zuul_version": "10.0.0", "pipelines": pipelines},
            )
        )
        result = json.loads(await get_status(mock_ctx, active_only=False))
        pipeline = result["pipelines"][0]
        assert pipeline["item_count"] == 50
        assert pipeline["pipeline_capped"] is True


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
        # GitLab MR ref fallback also returns empty
        respx.get(
            "https://zuul.example.com/api/tenant/test-tenant/status/change/"
            "refs%2Fmerge-requests%2F99999%2Fhead"
        ).mock(return_value=httpx.Response(200, json=[]))
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
    async def test_change_not_in_pipeline_timeout_graceful(self, mock_ctx):
        """Timeout during best-effort buildset fetch degrades gracefully."""
        respx.get("https://zuul.example.com/api/tenant/test-tenant/status/change/77777").mock(
            return_value=httpx.Response(200, json=[])
        )
        respx.get(
            "https://zuul.example.com/api/tenant/test-tenant/status/change/"
            "refs%2Fmerge-requests%2F77777%2Fhead"
        ).mock(return_value=httpx.Response(200, json=[]))
        respx.get("https://zuul.example.com/api/tenant/test-tenant/buildsets").mock(
            side_effect=httpx.ReadTimeout("timed out")
        )
        result = json.loads(await get_change_status(mock_ctx, "77777"))
        assert result["status"] == "not_in_pipeline"
        assert "latest_buildset" not in result

    @respx.mock
    async def test_change_not_in_pipeline_no_buildsets(self, mock_ctx):
        respx.get("https://zuul.example.com/api/tenant/test-tenant/status/change/88888").mock(
            return_value=httpx.Response(200, json=[])
        )
        # GitLab MR ref fallback also returns empty
        respx.get(
            "https://zuul.example.com/api/tenant/test-tenant/status/change/"
            "refs%2Fmerge-requests%2F88888%2Fhead"
        ).mock(return_value=httpx.Response(200, json=[]))
        respx.get("https://zuul.example.com/api/tenant/test-tenant/buildsets").mock(
            return_value=httpx.Response(200, json=[])
        )
        result = json.loads(await get_change_status(mock_ctx, "88888"))
        assert result["status"] == "not_in_pipeline"
        assert "latest_buildset" not in result

    @respx.mock
    async def test_not_in_pipeline_builds_have_report_url(self, mock_ctx):
        """not_in_pipeline builds should include report_url (Zuul web UI link)."""
        respx.get("https://zuul.example.com/api/tenant/test-tenant/status/change/44444").mock(
            return_value=httpx.Response(200, json=[])
        )
        respx.get(
            "https://zuul.example.com/api/tenant/test-tenant/status/change/"
            "refs%2Fmerge-requests%2F44444%2Fhead"
        ).mock(return_value=httpx.Response(200, json=[]))
        respx.get("https://zuul.example.com/api/tenant/test-tenant/buildsets").mock(
            return_value=httpx.Response(200, json=[{"uuid": "bs-url-test"}])
        )
        respx.get("https://zuul.example.com/api/tenant/test-tenant/buildset/bs-url-test").mock(
            return_value=httpx.Response(200, json=make_buildset(uuid="bs-url-test"))
        )
        result = json.loads(await get_change_status(mock_ctx, "44444"))
        build = result["latest_buildset"]["builds"][0]
        assert "report_url" in build, f"Missing report_url in not_in_pipeline build: {build}"
        assert build["report_url"] == ("https://zuul.example.com/t/test-tenant/build/build-uuid-1")

    @respx.mock
    async def test_not_in_pipeline_in_progress_has_status_hint(self, mock_ctx):
        """not_in_pipeline + IN_PROGRESS buildset should include a status_hint."""
        respx.get("https://zuul.example.com/api/tenant/test-tenant/status/change/55555").mock(
            return_value=httpx.Response(200, json=[])
        )
        respx.get(
            "https://zuul.example.com/api/tenant/test-tenant/status/change/"
            "refs%2Fmerge-requests%2F55555%2Fhead"
        ).mock(return_value=httpx.Response(200, json=[]))
        bs = make_buildset(uuid="bs-running", result="IN_PROGRESS")
        respx.get("https://zuul.example.com/api/tenant/test-tenant/buildsets").mock(
            return_value=httpx.Response(200, json=[{"uuid": "bs-running"}])
        )
        respx.get("https://zuul.example.com/api/tenant/test-tenant/buildset/bs-running").mock(
            return_value=httpx.Response(200, json=bs)
        )
        result = json.loads(await get_change_status(mock_ctx, "55555"))
        assert result["status"] == "not_in_pipeline"
        assert result["latest_buildset"]["result"] == "IN_PROGRESS"
        assert "status_hint" in result, "Expected status_hint for not_in_pipeline + IN_PROGRESS"
        assert "executing" in result["status_hint"].lower()

    @respx.mock
    async def test_not_in_pipeline_in_progress_builds_have_elapsed(self, mock_ctx):
        """IN_PROGRESS builds in not_in_pipeline path should include elapsed field."""
        from tests.conftest import make_build

        respx.get("https://zuul.example.com/api/tenant/test-tenant/status/change/77777").mock(
            return_value=httpx.Response(200, json=[])
        )
        respx.get(
            "https://zuul.example.com/api/tenant/test-tenant/status/change/"
            "refs%2Fmerge-requests%2F77777%2Fhead"
        ).mock(return_value=httpx.Response(200, json=[]))
        running_build = make_build(
            uuid="b-running",
            result=None,
            duration=None,
        )
        running_build["start_time"] = "2020-01-01T00:00:00"
        running_build["end_time"] = None
        bs = make_buildset(
            uuid="bs-running2",
            result="IN_PROGRESS",
            builds=[running_build],
        )
        respx.get("https://zuul.example.com/api/tenant/test-tenant/buildsets").mock(
            return_value=httpx.Response(200, json=[{"uuid": "bs-running2"}])
        )
        respx.get("https://zuul.example.com/api/tenant/test-tenant/buildset/bs-running2").mock(
            return_value=httpx.Response(200, json=bs)
        )
        result = json.loads(await get_change_status(mock_ctx, "77777"))
        assert result["status"] == "not_in_pipeline"
        builds = result["latest_buildset"]["builds"]
        assert len(builds) == 1
        assert builds[0]["result"] == "IN_PROGRESS"
        assert "elapsed" in builds[0], "IN_PROGRESS build should include elapsed"
        assert isinstance(builds[0]["elapsed"], str)

    @respx.mock
    async def test_not_in_pipeline_completed_no_status_hint(self, mock_ctx):
        """not_in_pipeline with a completed buildset should NOT include status_hint."""
        respx.get("https://zuul.example.com/api/tenant/test-tenant/status/change/66666").mock(
            return_value=httpx.Response(200, json=[])
        )
        respx.get(
            "https://zuul.example.com/api/tenant/test-tenant/status/change/"
            "refs%2Fmerge-requests%2F66666%2Fhead"
        ).mock(return_value=httpx.Response(200, json=[]))
        respx.get("https://zuul.example.com/api/tenant/test-tenant/buildsets").mock(
            return_value=httpx.Response(200, json=[{"uuid": "bs-done"}])
        )
        respx.get("https://zuul.example.com/api/tenant/test-tenant/buildset/bs-done").mock(
            return_value=httpx.Response(200, json=make_buildset(uuid="bs-done", result="SUCCESS"))
        )
        result = json.loads(await get_change_status(mock_ctx, "66666"))
        assert result["status"] == "not_in_pipeline"
        assert "status_hint" not in result

    @respx.mock
    async def test_gitlab_mr_ref_fallback(self, mock_ctx):
        """Digit-only change retries with refs/merge-requests/N/head for GitLab MRs."""
        respx.get("https://zuul.example.com/api/tenant/test-tenant/status/change/1925").mock(
            return_value=httpx.Response(200, json=[])
        )
        item = make_status_item(change=1925)
        item["refs"][0]["ref"] = "refs/merge-requests/1925/head"
        respx.get(
            "https://zuul.example.com/api/tenant/test-tenant/status/change/"
            "refs%2Fmerge-requests%2F1925%2Fhead"
        ).mock(return_value=httpx.Response(200, json=[item]))
        result = json.loads(await get_change_status(mock_ctx, "1925"))
        assert isinstance(result, list)
        assert len(result) == 1

    @respx.mock
    async def test_digit_change_not_in_pipeline_skips_full_scan(self, mock_ctx):
        """Digit-only change with no direct match goes to buildset lookup, not full scan."""
        respx.get("https://zuul.example.com/api/tenant/test-tenant/status/change/1925").mock(
            return_value=httpx.Response(200, json=[])
        )
        respx.get(
            "https://zuul.example.com/api/tenant/test-tenant/status/change/"
            "refs%2Fmerge-requests%2F1925%2Fhead"
        ).mock(return_value=httpx.Response(200, json=[]))
        respx.get("https://zuul.example.com/api/tenant/test-tenant/buildsets").mock(
            return_value=httpx.Response(200, json=[])
        )
        result = json.loads(await get_change_status(mock_ctx, "1925"))
        assert result["status"] == "not_in_pipeline"
        assert "latest_buildset" not in result

    @respx.mock
    async def test_digit_change_not_in_pipeline_fetches_buildset(self, mock_ctx):
        """Digit-only change not in pipeline fetches latest buildset."""
        respx.get("https://zuul.example.com/api/tenant/test-tenant/status/change/2001").mock(
            return_value=httpx.Response(200, json=[])
        )
        respx.get(
            "https://zuul.example.com/api/tenant/test-tenant/status/change/"
            "refs%2Fmerge-requests%2F2001%2Fhead"
        ).mock(return_value=httpx.Response(200, json=[]))
        respx.get("https://zuul.example.com/api/tenant/test-tenant/buildsets").mock(
            return_value=httpx.Response(200, json=[{"uuid": "bs-uuid-1"}])
        )
        respx.get("https://zuul.example.com/api/tenant/test-tenant/buildset/bs-uuid-1").mock(
            return_value=httpx.Response(
                200, json={"uuid": "bs-uuid-1", "result": "SUCCESS", "builds": []}
            )
        )
        result = json.loads(await get_change_status(mock_ctx, "2001"))
        assert result["status"] == "not_in_pipeline"
        assert "latest_buildset" in result

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
    async def test_elapsed_and_remaining_recomputed_for_running_jobs(self, mock_ctx):
        """Zuul's elapsed/remaining are stale snapshots. Verify we recompute both."""
        now = time.time()
        # Job started 600s (10 min) ago, but Zuul reports stale 60s elapsed
        # estimated_time=300s, so stale remaining = 300*1000-60000 = 240000ms
        item = make_status_item(change=55555)
        item["jobs"][0]["start_time"] = now - 600
        item["jobs"][0]["elapsed_time"] = 60000  # Stale: 60s in ms
        item["jobs"][0]["remaining_time"] = 240000  # Stale: 240s in ms
        item["jobs"][0]["estimated_time"] = 300  # 5 min in seconds
        item["jobs"][0]["result"] = None  # Still running
        respx.get("https://zuul.example.com/api/tenant/test-tenant/status/change/55555").mock(
            return_value=httpx.Response(200, json=[item])
        )
        result = json.loads(await get_change_status(mock_ctx, "55555"))
        job = result[0]["jobs"][0]
        # elapsed should be ~600s = "10m 0s" (recomputed from start_time)
        assert "10m" in job["elapsed"] or "9m" in job["elapsed"], (
            f"Expected ~10m, got {job['elapsed']}"
        )
        # remaining should be 0s (estimated 300s - elapsed 600s, clamped to 0)
        # NOT the stale "4m 0s" from Zuul
        assert job["remaining"] == "0s", f"Expected 0s (overdue), got {job['remaining']}"

    @respx.mock
    async def test_elapsed_preserved_for_completed_jobs(self, mock_ctx):
        """For completed jobs (with result), keep Zuul's elapsed value (converted to seconds)."""
        item = make_status_item(change=44444)
        item["jobs"][0]["start_time"] = 1704067200
        item["jobs"][0]["elapsed_time"] = 300000  # 5 min in ms
        item["jobs"][0]["result"] = "SUCCESS"
        respx.get("https://zuul.example.com/api/tenant/test-tenant/status/change/44444").mock(
            return_value=httpx.Response(200, json=[item])
        )
        result = json.loads(await get_change_status(mock_ctx, "44444"))
        elapsed = result[0]["jobs"][0]["elapsed"]
        assert elapsed == "5m 0s", "Completed job elapsed should be 5m 0s (300000ms / 1000)"

    async def test_no_change_no_url_returns_error(self, mock_ctx):
        result = json.loads(await get_change_status(mock_ctx))
        assert "error" in result

    @respx.mock
    async def test_running_job_has_status_running(self, mock_ctx):
        item = make_status_item(change=11111)
        # Default: result=None, start_time set, waiting_status=None → RUNNING
        respx.get("https://zuul.example.com/api/tenant/test-tenant/status/change/11111").mock(
            return_value=httpx.Response(200, json=[item])
        )
        result = json.loads(await get_change_status(mock_ctx, "11111"))
        assert result[0]["jobs"][0]["status"] == "RUNNING"

    @respx.mock
    async def test_waiting_job_has_status_waiting(self, mock_ctx):
        item = make_status_item(
            change=22222,
            jobs=[
                {
                    "name": "deploy-ocp",
                    "result": None,
                    "voting": True,
                    "waiting_status": "dependencies: deploy-infra",
                    "queued": False,
                    "tries": 0,
                }
            ],
        )
        respx.get("https://zuul.example.com/api/tenant/test-tenant/status/change/22222").mock(
            return_value=httpx.Response(200, json=[item])
        )
        result = json.loads(await get_change_status(mock_ctx, "22222"))
        assert result[0]["jobs"][0]["status"] == "WAITING"

    @respx.mock
    async def test_queued_job_has_status_queued(self, mock_ctx):
        item = make_status_item(
            change=33333,
            jobs=[
                {
                    "name": "test-job",
                    "uuid": "job-uuid-q",
                    "result": None,
                    "voting": True,
                    "queued": True,
                    "tries": 1,
                    "start_time": None,
                    "waiting_status": None,
                }
            ],
        )
        respx.get("https://zuul.example.com/api/tenant/test-tenant/status/change/33333").mock(
            return_value=httpx.Response(200, json=[item])
        )
        result = json.loads(await get_change_status(mock_ctx, "33333"))
        assert result[0]["jobs"][0]["status"] == "QUEUED"

    @respx.mock
    async def test_completed_job_has_result_as_status(self, mock_ctx):
        item = make_status_item(change=44400)
        item["jobs"][0]["result"] = "FAILURE"
        respx.get("https://zuul.example.com/api/tenant/test-tenant/status/change/44400").mock(
            return_value=httpx.Response(200, json=[item])
        )
        result = json.loads(await get_change_status(mock_ctx, "44400"))
        assert result[0]["jobs"][0]["status"] == "FAILURE"

    @respx.mock
    async def test_relative_stream_url_made_absolute(self, mock_ctx):
        item = make_status_item(change=70001)
        item["jobs"][0]["url"] = "stream/job-uuid-1?logfile=console.log"
        respx.get("https://zuul.example.com/api/tenant/test-tenant/status/change/70001").mock(
            return_value=httpx.Response(200, json=[item])
        )
        result = json.loads(await get_change_status(mock_ctx, "70001"))
        stream_url = result[0]["jobs"][0]["stream_url"]
        assert stream_url == (
            "https://zuul.example.com/t/test-tenant/stream/job-uuid-1?logfile=console.log"
        )

    @respx.mock
    async def test_absolute_stream_url_unchanged(self, mock_ctx):
        item = make_status_item(change=70002)
        item["jobs"][0]["url"] = "wss://zuul.example.com/console"
        respx.get("https://zuul.example.com/api/tenant/test-tenant/status/change/70002").mock(
            return_value=httpx.Response(200, json=[item])
        )
        result = json.loads(await get_change_status(mock_ctx, "70002"))
        assert result[0]["jobs"][0]["stream_url"] == "wss://zuul.example.com/console"

    @respx.mock
    async def test_chain_summary_present(self, mock_ctx):
        item = make_status_item(change=80001)
        respx.get("https://zuul.example.com/api/tenant/test-tenant/status/change/80001").mock(
            return_value=httpx.Response(200, json=[item])
        )
        result = json.loads(await get_change_status(mock_ctx, "80001"))
        summary = result[0]["chain_summary"]
        assert summary["total"] == 1
        assert summary["running"] == 1
        assert summary["completed"] == 0

    @respx.mock
    async def test_enqueue_time_normalized_to_seconds(self, mock_ctx):
        item = make_status_item(change=80002)
        # conftest sets enqueue_time=1704067200000 (ms)
        respx.get("https://zuul.example.com/api/tenant/test-tenant/status/change/80002").mock(
            return_value=httpx.Response(200, json=[item])
        )
        result = json.loads(await get_change_status(mock_ctx, "80002"))
        assert result[0]["enqueue_time"] == 1704067200.0  # seconds

    # ---- brief mode tests ----

    @respx.mock
    async def test_brief_in_pipeline_strips_job_fields(self, mock_ctx):
        """brief=True should strip static fields from in-pipeline jobs."""
        item = make_status_item(change=90001)
        respx.get("https://zuul.example.com/api/tenant/test-tenant/status/change/90001").mock(
            return_value=httpx.Response(200, json=[item])
        )
        result = json.loads(await get_change_status(mock_ctx, "90001", brief=True))
        assert isinstance(result, list)
        job = result[0]["jobs"][0]
        # Essential fields preserved
        assert "name" in job
        assert "status" in job
        assert "elapsed" in job
        # Static fields stripped
        assert "uuid" not in job
        assert "stream_url" not in job
        assert "dependencies" not in job
        assert "waiting_status" not in job
        assert "remaining" not in job
        assert "estimated" not in job
        assert "report_url" not in job
        # Item-level static fields stripped
        assert "status_url" not in result[0]
        assert "enqueue_time" not in result[0]
        # chain_summary preserved (compact, useful for monitoring)
        assert "chain_summary" in result[0]

    @respx.mock
    async def test_brief_in_pipeline_smaller_than_full(self, mock_ctx):
        """brief=True response should be substantially smaller than brief=False."""
        item = make_chained_status_item(change=90002)
        respx.get("https://zuul.example.com/api/tenant/test-tenant/status/change/90002").mock(
            return_value=httpx.Response(200, json=[item])
        )
        full = await get_change_status(mock_ctx, "90002", brief=False)
        brief = await get_change_status(mock_ctx, "90002", brief=True)
        full_size = len(full.encode())
        brief_size = len(brief.encode())
        savings_pct = (1 - brief_size / full_size) * 100
        assert savings_pct > 20, f"Brief saves only {savings_pct:.0f}% — expected >20%"

    @respx.mock
    async def test_brief_not_in_pipeline_strips_build_fields(self, mock_ctx):
        """brief=True should use abbreviated builds for not_in_pipeline path."""

        respx.get("https://zuul.example.com/api/tenant/test-tenant/status/change/90003").mock(
            return_value=httpx.Response(200, json=[])
        )
        respx.get(
            "https://zuul.example.com/api/tenant/test-tenant/status/change/"
            "refs%2Fmerge-requests%2F90003%2Fhead"
        ).mock(return_value=httpx.Response(200, json=[]))
        respx.get("https://zuul.example.com/api/tenant/test-tenant/buildsets").mock(
            return_value=httpx.Response(200, json=[{"uuid": "bs-brief"}])
        )
        respx.get("https://zuul.example.com/api/tenant/test-tenant/buildset/bs-brief").mock(
            return_value=httpx.Response(200, json=make_buildset(uuid="bs-brief"))
        )
        result = json.loads(await get_change_status(mock_ctx, "90003", brief=True))
        assert result["status"] == "not_in_pipeline"
        bs = result["latest_buildset"]
        # Buildset should have builds
        assert "builds" in bs
        build = bs["builds"][0]
        # Brief build fields present
        assert "job" in build
        assert "result" in build
        assert "uuid" in build
        # Full-detail fields stripped (brief=True on fmt_build)
        assert "log_url" not in build
        assert "nodeset" not in build
        assert "artifacts" not in build
        assert "patchset" not in build
        assert "branch" not in build
        # Buildset-level detail stripped
        assert "message" not in bs
        assert "events" not in bs

    @respx.mock
    async def test_brief_not_in_pipeline_smaller_than_full(self, mock_ctx):
        """brief=True not_in_pipeline response should be smaller than full."""
        from tests.conftest import make_build

        respx.get("https://zuul.example.com/api/tenant/test-tenant/status/change/90004").mock(
            return_value=httpx.Response(200, json=[])
        )
        respx.get(
            "https://zuul.example.com/api/tenant/test-tenant/status/change/"
            "refs%2Fmerge-requests%2F90004%2Fhead"
        ).mock(return_value=httpx.Response(200, json=[]))
        bs = make_buildset(
            uuid="bs-size",
            builds=[make_build(uuid=f"b-{i}", job_name=f"job-{i}") for i in range(5)],
        )
        respx.get("https://zuul.example.com/api/tenant/test-tenant/buildsets").mock(
            return_value=httpx.Response(200, json=[{"uuid": "bs-size"}])
        )
        respx.get("https://zuul.example.com/api/tenant/test-tenant/buildset/bs-size").mock(
            return_value=httpx.Response(200, json=bs)
        )
        full = await get_change_status(mock_ctx, "90004", brief=False)
        brief = await get_change_status(mock_ctx, "90004", brief=True)
        full_size = len(full.encode())
        brief_size = len(brief.encode())
        savings_pct = (1 - brief_size / full_size) * 100
        assert savings_pct > 30, f"Brief saves only {savings_pct:.0f}% — expected >30%"

    @respx.mock
    async def test_brief_not_in_pipeline_still_has_report_url(self, mock_ctx):
        """brief=True should still enrich builds with report_url."""
        respx.get("https://zuul.example.com/api/tenant/test-tenant/status/change/90005").mock(
            return_value=httpx.Response(200, json=[])
        )
        respx.get(
            "https://zuul.example.com/api/tenant/test-tenant/status/change/"
            "refs%2Fmerge-requests%2F90005%2Fhead"
        ).mock(return_value=httpx.Response(200, json=[]))
        respx.get("https://zuul.example.com/api/tenant/test-tenant/buildsets").mock(
            return_value=httpx.Response(200, json=[{"uuid": "bs-rurl"}])
        )
        respx.get("https://zuul.example.com/api/tenant/test-tenant/buildset/bs-rurl").mock(
            return_value=httpx.Response(200, json=make_buildset(uuid="bs-rurl"))
        )
        result = json.loads(await get_change_status(mock_ctx, "90005", brief=True))
        build = result["latest_buildset"]["builds"][0]
        assert "report_url" in build

    @respx.mock
    async def test_brief_not_in_pipeline_in_progress_has_elapsed(self, mock_ctx):
        """brief=True should compute elapsed for IN_PROGRESS builds."""
        from tests.conftest import make_build

        respx.get("https://zuul.example.com/api/tenant/test-tenant/status/change/90006").mock(
            return_value=httpx.Response(200, json=[])
        )
        respx.get(
            "https://zuul.example.com/api/tenant/test-tenant/status/change/"
            "refs%2Fmerge-requests%2F90006%2Fhead"
        ).mock(return_value=httpx.Response(200, json=[]))
        running_build = make_build(uuid="b-run", result=None, duration=None)
        running_build["start_time"] = "2020-01-01T00:00:00"
        running_build["end_time"] = None
        bs = make_buildset(uuid="bs-brief-run", result="IN_PROGRESS", builds=[running_build])
        respx.get("https://zuul.example.com/api/tenant/test-tenant/buildsets").mock(
            return_value=httpx.Response(200, json=[{"uuid": "bs-brief-run"}])
        )
        respx.get("https://zuul.example.com/api/tenant/test-tenant/buildset/bs-brief-run").mock(
            return_value=httpx.Response(200, json=bs)
        )
        result = json.loads(await get_change_status(mock_ctx, "90006", brief=True))
        assert result["status"] == "not_in_pipeline"
        builds = result["latest_buildset"]["builds"]
        assert len(builds) == 1
        assert builds[0]["result"] == "IN_PROGRESS"
        assert "elapsed" in builds[0], "brief IN_PROGRESS build should include elapsed"
        assert isinstance(builds[0]["elapsed"], str)
        # Should also have report_url
        assert "report_url" in builds[0]
        # Should NOT have full-detail fields
        assert "log_url" not in builds[0]
        assert "nodeset" not in builds[0]
        assert "artifacts" not in builds[0]

    # ---- chain_summary in not_in_pipeline tests ----

    @respx.mock
    async def test_not_in_pipeline_all_completed_has_chain_summary(self, mock_ctx):
        """Completed buildset in not_in_pipeline should include chain_summary."""
        from tests.conftest import make_build

        respx.get("https://zuul.example.com/api/tenant/test-tenant/status/change/91001").mock(
            return_value=httpx.Response(200, json=[])
        )
        respx.get(
            "https://zuul.example.com/api/tenant/test-tenant/status/change/"
            "refs%2Fmerge-requests%2F91001%2Fhead"
        ).mock(return_value=httpx.Response(200, json=[]))
        builds = [
            make_build(uuid=f"b-{i}", job_name=f"job-{i}", result="SUCCESS") for i in range(3)
        ]
        bs = make_buildset(uuid="bs-chain-done", result="SUCCESS", builds=builds)
        respx.get("https://zuul.example.com/api/tenant/test-tenant/buildsets").mock(
            return_value=httpx.Response(200, json=[{"uuid": "bs-chain-done"}])
        )
        respx.get("https://zuul.example.com/api/tenant/test-tenant/buildset/bs-chain-done").mock(
            return_value=httpx.Response(200, json=bs)
        )
        result = json.loads(await get_change_status(mock_ctx, "91001"))
        assert result["status"] == "not_in_pipeline"
        summary = result["chain_summary"]
        assert summary["completed"] == 3
        assert summary["total"] == 3
        assert summary["running"] == 0
        assert summary["waiting"] == 0
        assert summary["progress_pct"] == 100
        assert summary["all_decided"] is True

    @respx.mock
    async def test_not_in_pipeline_mixed_has_chain_summary(self, mock_ctx):
        """Mixed results (some done, some running) should show partial progress."""
        from tests.conftest import make_build

        respx.get("https://zuul.example.com/api/tenant/test-tenant/status/change/91002").mock(
            return_value=httpx.Response(200, json=[])
        )
        respx.get(
            "https://zuul.example.com/api/tenant/test-tenant/status/change/"
            "refs%2Fmerge-requests%2F91002%2Fhead"
        ).mock(return_value=httpx.Response(200, json=[]))
        done = make_build(uuid="b-done", job_name="deploy-infra", result="SUCCESS")
        running1 = make_build(uuid="b-run1", job_name="deploy-ocp", result=None, duration=None)
        running1["start_time"] = "2020-01-01T00:00:00"
        running1["end_time"] = None
        running2 = make_build(uuid="b-run2", job_name="deploy-osp", result=None, duration=None)
        running2["start_time"] = "2020-01-01T00:00:00"
        running2["end_time"] = None
        bs = make_buildset(
            uuid="bs-chain-mixed",
            result="IN_PROGRESS",
            builds=[done, running1, running2],
        )
        respx.get("https://zuul.example.com/api/tenant/test-tenant/buildsets").mock(
            return_value=httpx.Response(200, json=[{"uuid": "bs-chain-mixed"}])
        )
        respx.get("https://zuul.example.com/api/tenant/test-tenant/buildset/bs-chain-mixed").mock(
            return_value=httpx.Response(200, json=bs)
        )
        result = json.loads(await get_change_status(mock_ctx, "91002"))
        summary = result["chain_summary"]
        assert summary["completed"] == 1
        assert summary["total"] == 3
        assert summary["running"] == 2
        assert summary["progress_pct"] == 33
        assert summary["all_decided"] is False

    @respx.mock
    async def test_not_in_pipeline_brief_has_chain_summary(self, mock_ctx):
        """brief=True not_in_pipeline should also include chain_summary."""
        from tests.conftest import make_build

        respx.get("https://zuul.example.com/api/tenant/test-tenant/status/change/91003").mock(
            return_value=httpx.Response(200, json=[])
        )
        respx.get(
            "https://zuul.example.com/api/tenant/test-tenant/status/change/"
            "refs%2Fmerge-requests%2F91003%2Fhead"
        ).mock(return_value=httpx.Response(200, json=[]))
        builds = [
            make_build(uuid="b-0", job_name="job-0", result="SUCCESS"),
            make_build(uuid="b-1", job_name="job-1", result="FAILURE"),
            make_build(uuid="b-2", job_name="job-2", result=None, duration=None),
        ]
        builds[2]["start_time"] = "2020-01-01T00:00:00"
        builds[2]["end_time"] = None
        bs = make_buildset(uuid="bs-brief-chain", result="IN_PROGRESS", builds=builds)
        respx.get("https://zuul.example.com/api/tenant/test-tenant/buildsets").mock(
            return_value=httpx.Response(200, json=[{"uuid": "bs-brief-chain"}])
        )
        respx.get("https://zuul.example.com/api/tenant/test-tenant/buildset/bs-brief-chain").mock(
            return_value=httpx.Response(200, json=bs)
        )
        result = json.loads(await get_change_status(mock_ctx, "91003", brief=True))
        assert result["status"] == "not_in_pipeline"
        summary = result["chain_summary"]
        assert summary["completed"] == 2  # SUCCESS + FAILURE
        assert summary["total"] == 3
        assert summary["running"] == 1
        assert summary["progress_pct"] == 67
        assert summary["all_decided"] is False

    @respx.mock
    async def test_not_in_pipeline_no_builds_chain_summary(self, mock_ctx):
        """Buildset with empty builds should have chain_summary with total=0."""
        respx.get("https://zuul.example.com/api/tenant/test-tenant/status/change/91004").mock(
            return_value=httpx.Response(200, json=[])
        )
        respx.get(
            "https://zuul.example.com/api/tenant/test-tenant/status/change/"
            "refs%2Fmerge-requests%2F91004%2Fhead"
        ).mock(return_value=httpx.Response(200, json=[]))
        # Build the response directly — make_buildset(builds=[]) falls through
        # to default because [] is falsy in Python.
        bs = {
            "uuid": "bs-empty-builds",
            "result": "SUCCESS",
            "pipeline": "check",
            "refs": [{"project": "org/repo", "change": 91004}],
            "builds": [],
        }
        respx.get("https://zuul.example.com/api/tenant/test-tenant/buildsets").mock(
            return_value=httpx.Response(200, json=[{"uuid": "bs-empty-builds"}])
        )
        respx.get("https://zuul.example.com/api/tenant/test-tenant/buildset/bs-empty-builds").mock(
            return_value=httpx.Response(200, json=bs)
        )
        result = json.loads(await get_change_status(mock_ctx, "91004"))
        summary = result["chain_summary"]
        assert summary["total"] == 0
        assert summary["progress_pct"] == 0
        assert summary["all_decided"] is False

    @respx.mock
    async def test_not_in_pipeline_all_failed_chain_summary(self, mock_ctx):
        """All-failed buildset: all_decided=True, progress_pct=100."""
        from tests.conftest import make_build

        respx.get("https://zuul.example.com/api/tenant/test-tenant/status/change/91005").mock(
            return_value=httpx.Response(200, json=[])
        )
        respx.get(
            "https://zuul.example.com/api/tenant/test-tenant/status/change/"
            "refs%2Fmerge-requests%2F91005%2Fhead"
        ).mock(return_value=httpx.Response(200, json=[]))
        builds = [
            make_build(uuid="b-f0", job_name="job-0", result="FAILURE"),
            make_build(uuid="b-f1", job_name="job-1", result="NODE_FAILURE"),
            make_build(uuid="b-f2", job_name="job-2", result="TIMED_OUT"),
        ]
        bs = make_buildset(uuid="bs-all-fail", result="FAILURE", builds=builds)
        respx.get("https://zuul.example.com/api/tenant/test-tenant/buildsets").mock(
            return_value=httpx.Response(200, json=[{"uuid": "bs-all-fail"}])
        )
        respx.get("https://zuul.example.com/api/tenant/test-tenant/buildset/bs-all-fail").mock(
            return_value=httpx.Response(200, json=bs)
        )
        result = json.loads(await get_change_status(mock_ctx, "91005"))
        summary = result["chain_summary"]
        assert summary["completed"] == 3
        assert summary["total"] == 3
        assert summary["all_decided"] is True
        assert summary["progress_pct"] == 100

    # ---- exception logging in not_in_pipeline fallback ----

    @respx.mock
    async def test_not_in_pipeline_value_error_logged_not_raised(self, mock_ctx, caplog):
        """ValueError in fallback path should be logged but not raise."""
        import logging

        respx.get("https://zuul.example.com/api/tenant/test-tenant/status/change/92001").mock(
            return_value=httpx.Response(200, json=[])
        )
        respx.get(
            "https://zuul.example.com/api/tenant/test-tenant/status/change/"
            "refs%2Fmerge-requests%2F92001%2Fhead"
        ).mock(return_value=httpx.Response(200, json=[]))
        respx.get("https://zuul.example.com/api/tenant/test-tenant/buildsets").mock(
            return_value=httpx.Response(200, json=[{"uuid": "bs-err"}])
        )
        # Return data that triggers ValueError in fmt_buildset (non-JSON response mock)
        respx.get("https://zuul.example.com/api/tenant/test-tenant/buildset/bs-err").mock(
            return_value=httpx.Response(
                200, content=b"not json", headers={"content-type": "text/html"}
            )
        )
        with caplog.at_level(logging.WARNING, logger="zuul-mcp"):
            result = json.loads(await get_change_status(mock_ctx, "92001"))
        # Should degrade gracefully
        assert result["status"] == "not_in_pipeline"
        assert "latest_buildset" not in result
        # ValueError should be logged
        assert any(
            "not_in_pipeline" in r.message.lower() or "ValueError" in r.message
            for r in caplog.records
            if r.levelno >= logging.WARNING
        )


class TestFormatDuration:
    def test_seconds_only(self):
        assert _format_duration(45) == "45s"

    def test_minutes_and_seconds(self):
        assert _format_duration(125) == "2m 5s"

    def test_hours_and_minutes(self):
        assert _format_duration(3723) == "1h 2m"

    def test_hours_only(self):
        assert _format_duration(7200) == "2h 0m"

    def test_zero(self):
        assert _format_duration(0) == "0s"

    def test_none_returns_none(self):
        assert _format_duration(None) is None

    def test_large_duration(self):
        assert _format_duration(36000) == "10h 0m"

    def test_negative_clamped_to_zero(self):
        """Negative durations (clock skew) should clamp to 0s."""
        assert _format_duration(-5) == "0s"
        assert _format_duration(-61) == "0s"
        assert _format_duration(-3601) == "0s"

    def test_float_truncated(self):
        assert _format_duration(0.7) == "0s"
        assert _format_duration(65.9) == "1m 5s"

    def test_inf_returns_none(self):
        assert _format_duration(float("inf")) is None

    def test_nan_returns_none(self):
        assert _format_duration(float("nan")) is None


class TestBuildsetChainSummary:
    """Unit tests for _buildset_chain_summary (not_in_pipeline chain tracking)."""

    def test_all_success(self):
        from mcp_zuul.tools._status import _buildset_chain_summary

        builds = [
            {"result": "SUCCESS"},
            {"result": "SUCCESS"},
            {"result": "SUCCESS"},
        ]
        s = _buildset_chain_summary(builds)
        assert s == {
            "completed": 3,
            "total": 3,
            "running": 0,
            "waiting": 0,
            "progress_pct": 100,
            "all_decided": True,
        }

    def test_mixed_terminal_and_running(self):
        from mcp_zuul.tools._status import _buildset_chain_summary

        builds = [
            {"result": "SUCCESS"},
            {"result": "IN_PROGRESS"},
            {"result": "FAILURE"},
        ]
        s = _buildset_chain_summary(builds)
        assert s["completed"] == 2
        assert s["running"] == 1
        assert s["progress_pct"] == 67
        assert s["all_decided"] is False

    def test_all_running(self):
        from mcp_zuul.tools._status import _buildset_chain_summary

        builds = [{"result": "IN_PROGRESS"}, {"result": "IN_PROGRESS"}]
        s = _buildset_chain_summary(builds)
        assert s["completed"] == 0
        assert s["running"] == 2
        assert s["progress_pct"] == 0
        assert s["all_decided"] is False

    def test_empty_list(self):
        from mcp_zuul.tools._status import _buildset_chain_summary

        s = _buildset_chain_summary([])
        assert s["total"] == 0
        assert s["all_decided"] is False

    def test_all_terminal_results_recognized(self):
        """Every result in _TERMINAL_RESULTS should count as completed."""
        from mcp_zuul.formatters import _TERMINAL_RESULTS
        from mcp_zuul.tools._status import _buildset_chain_summary

        builds = [{"result": r} for r in _TERMINAL_RESULTS]
        s = _buildset_chain_summary(builds)
        assert s["completed"] == len(_TERMINAL_RESULTS)
        assert s["running"] == 0
        assert s["all_decided"] is True

    def test_unknown_result_treated_as_running(self):
        """A result not in _TERMINAL_RESULTS should count as running."""
        from mcp_zuul.tools._status import _buildset_chain_summary

        builds = [{"result": "UNKNOWN_FUTURE_RESULT"}, {"result": "SUCCESS"}]
        s = _buildset_chain_summary(builds)
        assert s["completed"] == 1
        assert s["running"] == 1

    def test_single_build(self):
        from mcp_zuul.tools._status import _buildset_chain_summary

        s = _buildset_chain_summary([{"result": "FAILURE"}])
        assert s == {
            "completed": 1,
            "total": 1,
            "running": 0,
            "waiting": 0,
            "progress_pct": 100,
            "all_decided": True,
        }

    def test_build_missing_result_key(self):
        """Build dict without 'result' key should count as running."""
        from mcp_zuul.tools._status import _buildset_chain_summary

        builds = [{"job": "deploy-infra"}, {"result": "SUCCESS"}]
        s = _buildset_chain_summary(builds)
        assert s["completed"] == 1
        assert s["running"] == 1

    def test_progress_rounding(self):
        """1/3 = 33.33...% should round to 33."""
        from mcp_zuul.tools._status import _buildset_chain_summary

        builds = [
            {"result": "SUCCESS"},
            {"result": "IN_PROGRESS"},
            {"result": "IN_PROGRESS"},
        ]
        assert _buildset_chain_summary(builds)["progress_pct"] == 33

    def test_progress_rounding_two_thirds(self):
        """2/3 = 66.66...% should round to 67."""
        from mcp_zuul.tools._status import _buildset_chain_summary

        builds = [
            {"result": "SUCCESS"},
            {"result": "FAILURE"},
            {"result": "IN_PROGRESS"},
        ]
        assert _buildset_chain_summary(builds)["progress_pct"] == 67


class TestChainSummary:
    def test_chain_progress(self):
        from mcp_zuul.formatters import fmt_status_item

        item = make_chained_status_item()
        formatted = fmt_status_item(item)
        summary = formatted["chain_summary"]
        assert summary["completed"] == 1  # deploy-infra
        assert summary["total"] == 7
        assert summary["running"] == 2  # deploy-ocp + deploy-osp
        assert summary["waiting"] == 4
        assert 0 < summary["progress_pct"] < 100

    def test_critical_path_remaining(self):
        from mcp_zuul.formatters import _compute_chain_summary, fmt_status_item

        item = make_chained_status_item()
        formatted = fmt_status_item(item)
        summary = formatted["chain_summary"]
        # cp_eta is human-readable, verify it shows hours
        assert "h" in summary["cp_eta"]
        # Also verify the numeric computation via internal function
        # (fmt_status_item strips _-prefixed numeric fields, so test via _compute_chain_summary)
        import time as _t

        now = _t.time()
        from mcp_zuul.formatters import _format_job

        jobs = [_format_job(j, now) for j in item["jobs"]]
        raw_summary = _compute_chain_summary(jobs)
        assert raw_summary["critical_path_remaining"] > 20000  # > ~5.5h
        assert raw_summary["critical_path_remaining"] < 35000  # < ~9.7h

    def test_all_completed(self):
        from mcp_zuul.formatters import fmt_status_item

        item = make_chained_status_item()
        for j in item["jobs"]:
            j["result"] = "SUCCESS"
            j["elapsed_time"] = 300000
            j.pop("remaining_time", None)
            j.pop("waiting_status", None)
            j.pop("estimated_time", None)
        formatted = fmt_status_item(item)
        summary = formatted["chain_summary"]
        assert summary["completed"] == 7
        assert summary["progress_pct"] == 100
        assert summary["cp_eta"] == "0s"

    def test_single_job(self):
        from mcp_zuul.formatters import fmt_status_item

        item = make_status_item()
        formatted = fmt_status_item(item)
        summary = formatted["chain_summary"]
        assert summary["total"] == 1
        assert summary["running"] == 1

    def test_no_estimated_time_uses_zero(self):
        from mcp_zuul.formatters import fmt_status_item

        item = make_chained_status_item()
        for j in item["jobs"]:
            j.pop("estimated_time", None)
        formatted = fmt_status_item(item)
        summary = formatted["chain_summary"]
        assert summary["cp_eta"] == "0s"

    def test_empty_jobs(self):
        """Item with no jobs still gets a chain_summary."""
        from mcp_zuul.formatters import fmt_status_item

        item = make_status_item()
        item["jobs"] = []
        formatted = fmt_status_item(item)
        assert formatted["chain_summary"]["total"] == 0
        assert formatted["chain_summary"]["cp_eta"] == "0s"

    def test_dict_dependencies_handled(self):
        """Dependencies as dicts (e.g. from future Zuul API) should not crash."""
        from mcp_zuul.formatters import _compute_chain_summary

        jobs = [
            {
                "name": "job-a",
                "status": "RUNNING",
                "result": None,
                "_remaining_secs": 100,
                "_estimated_secs": 200,
                "_elapsed_secs": 100,
            },
            {
                "name": "job-b",
                "status": "WAITING",
                "result": None,
                "_remaining_secs": None,
                "_estimated_secs": 300,
                "_elapsed_secs": 0,
                "dependencies": [{"name": "job-a", "soft": False}],
            },
        ]
        summary = _compute_chain_summary(jobs)
        assert summary["critical_path_remaining"] > 0

    def test_malformed_dependency_dict_no_name(self):
        """Dependency dict missing 'name' key should not crash."""
        from mcp_zuul.formatters import _compute_chain_summary

        jobs = [
            {
                "name": "job-a",
                "status": "RUNNING",
                "result": None,
                "_remaining_secs": 50,
                "_estimated_secs": 100,
                "_elapsed_secs": 50,
            },
            {
                "name": "job-b",
                "status": "WAITING",
                "result": None,
                "_remaining_secs": None,
                "_estimated_secs": 200,
                "_elapsed_secs": 0,
                # Malformed: dict without "name" key
                "dependencies": [{"soft": False}],
            },
        ]
        summary = _compute_chain_summary(jobs)
        # job-b has estimated=200 but its dep resolves to "" (unknown) → 0
        assert summary["critical_path_remaining"] == 200
        assert summary["total"] == 2

    def test_cycle_detection(self):
        """Circular dependencies don't cause infinite recursion."""
        jobs = [
            {
                "name": "a",
                "status": "WAITING",
                "_estimated_secs": 100,
                "_remaining_secs": None,
                "_elapsed_secs": 0,
                "dependencies": ["b"],
                "waiting_status": "b",
            },
            {
                "name": "b",
                "status": "WAITING",
                "_estimated_secs": 200,
                "_remaining_secs": None,
                "_elapsed_secs": 0,
                "dependencies": ["a"],
                "waiting_status": "a",
            },
        ]
        summary = _compute_chain_summary(jobs)
        # Should not hang or raise RecursionError
        assert summary["total"] == 2
        # With cycle broken (one dep resolves to 0), critical path = max(a_own, b_own)
        # a_own = 100 + dep_b, b_own = 200 + dep_a; cycle breaks one to 0
        assert summary["critical_path_remaining"] > 0

    def test_negative_remaining_clamped(self):
        """Overdue RUNNING jobs (negative remaining) don't produce negative ETA."""
        jobs = [
            {
                "name": "overdue",
                "status": "RUNNING",
                "_remaining_secs": -60,
                "_elapsed_secs": 7200,
                "_estimated_secs": 7140,
            },
        ]
        summary = _compute_chain_summary(jobs)
        assert summary["critical_path_remaining"] == 0

    def test_clock_skew_elapsed_clamped(self):
        """Negative elapsed from clock skew is clamped to 0."""
        from mcp_zuul.formatters import fmt_status_item

        item = make_status_item(change=90001)
        item["jobs"][0]["start_time"] = time.time() + 10  # future (clock skew)
        item["jobs"][0]["result"] = None
        formatted = fmt_status_item(item)
        assert formatted["jobs"][0]["elapsed"] == "0s"

    def test_remaining_recomputed_for_running_jobs(self):
        """Running jobs get fresh remaining from estimated - elapsed, not stale Zuul value."""
        from mcp_zuul.formatters import fmt_status_item

        now = time.time()
        item = make_status_item(change=90002)
        # Job started 60m ago, estimated 109m, Zuul says remaining=96m (stale from 13m ago)
        item["jobs"][0]["start_time"] = now - 3600  # 60m ago
        item["jobs"][0]["elapsed_time"] = 780000  # Stale: 13m in ms
        item["jobs"][0]["remaining_time"] = 5760000  # Stale: 96m in ms
        item["jobs"][0]["estimated_time"] = 6540  # 109m in seconds
        item["jobs"][0]["result"] = None
        formatted = fmt_status_item(item)
        job = formatted["jobs"][0]
        # Fresh remaining = estimated(6540) - elapsed(3600) = 2940s = 49m 0s
        # NOT the stale "96m 0s" from Zuul
        assert job["remaining"] == "49m 0s", f"Expected 49m 0s, got {job['remaining']}"

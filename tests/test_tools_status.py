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
    async def test_digit_change_not_in_pipeline_skips_full_scan(self, mock_ctx):
        """Digit-only change with no direct match goes to buildset lookup, not full scan."""
        respx.get("https://zuul.example.com/api/tenant/test-tenant/status/change/1925").mock(
            return_value=httpx.Response(200, json=[])
        )
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

    def test_cycle_detection(self):
        """Circular dependencies don't cause infinite recursion."""
        jobs = [
            {
                "name": "a",
                "status": "WAITING",
                "estimated": 100,
                "dependencies": ["b"],
                "waiting_status": "b",
            },
            {
                "name": "b",
                "status": "WAITING",
                "estimated": 200,
                "dependencies": ["a"],
                "waiting_status": "a",
            },
        ]
        summary = _compute_chain_summary(jobs)
        # Should not hang or raise RecursionError
        assert summary["total"] == 2
        assert summary["critical_path_remaining"] >= 0

    def test_negative_remaining_clamped(self):
        """Overdue RUNNING jobs (negative remaining) don't produce negative ETA."""
        jobs = [
            {
                "name": "overdue",
                "status": "RUNNING",
                "remaining": -60,
                "elapsed": 7200,
                "estimated": 7140,
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

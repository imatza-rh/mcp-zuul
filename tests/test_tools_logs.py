"""Integration tests for log tools (get_build_log, browse_build_logs)."""

import json

import httpx
import respx

from mcp_zuul.tools import browse_build_logs, get_build_log, tail_build_log
from tests.conftest import make_build

_SAMPLE_LOG = "\n".join([f"line {i}: content for line {i}" for i in range(1, 201)])
_SAMPLE_LOG_WITH_ERRORS = "\n".join(
    [
        "2025-01-01 task ok",
        "2025-01-01 FAILED! => some error",
        "2025-01-01 ok=5 changed=2 failed=0",
        "2025-01-01 fatal: connection refused",
        "2025-01-01 task completed",
        "2025-01-01 Traceback (most recent call last):",
        "2025-01-01 ok=3 changed=1 failed=1",
    ]
)


class TestGetBuildLog:
    @respx.mock
    async def test_summary_mode(self, mock_ctx):
        build = make_build()
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/build-uuid-1").mock(
            return_value=httpx.Response(200, json=build)
        )
        respx.get(f"{build['log_url']}job-output.txt").mock(
            return_value=httpx.Response(200, text=_SAMPLE_LOG_WITH_ERRORS)
        )
        result = json.loads(await get_build_log(mock_ctx, "build-uuid-1"))
        assert result["total_lines"] == 7
        assert result["job"] == "test-job"
        # Should find FAILED!, fatal:, Traceback, failed=1 as errors
        error_texts = [e["text"] for e in result["error_lines"]]
        assert any("FAILED!" in t for t in error_texts)
        assert any("fatal:" in t for t in error_texts)
        assert any("Traceback" in t for t in error_texts)
        # failed=0 should be filtered by noise pattern
        assert not any("failed=0" in t for t in error_texts)

    @respx.mock
    async def test_line_range_mode(self, mock_ctx):
        build = make_build()
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/build-uuid-1").mock(
            return_value=httpx.Response(200, json=build)
        )
        respx.get(f"{build['log_url']}job-output.txt").mock(
            return_value=httpx.Response(200, text=_SAMPLE_LOG)
        )
        result = json.loads(
            await get_build_log(mock_ctx, "build-uuid-1", start_line=10, end_line=15)
        )
        assert result["count"] == 6
        assert result["start_line"] == 10
        assert result["end_line"] == 15
        assert result["lines"][0]["n"] == 10

    @respx.mock
    async def test_grep_mode(self, mock_ctx):
        build = make_build()
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/build-uuid-1").mock(
            return_value=httpx.Response(200, json=build)
        )
        respx.get(f"{build['log_url']}job-output.txt").mock(
            return_value=httpx.Response(200, text=_SAMPLE_LOG)
        )
        result = json.loads(await get_build_log(mock_ctx, "build-uuid-1", grep="line 5[0-9]:"))
        assert result["grep"] == "line 5[0-9]:"
        assert result["matched"] == 10  # lines 50-59

    @respx.mock
    async def test_grep_with_context(self, mock_ctx):
        build = make_build()
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/build-uuid-1").mock(
            return_value=httpx.Response(200, json=build)
        )
        log_text = "\n".join(["before", "ERROR here", "after"])
        respx.get(f"{build['log_url']}job-output.txt").mock(
            return_value=httpx.Response(200, text=log_text)
        )
        result = json.loads(await get_build_log(mock_ctx, "build-uuid-1", grep="ERROR", context=1))
        assert "blocks" in result
        assert len(result["blocks"]) == 1
        # Block should contain before, ERROR, after
        block = result["blocks"][0]
        assert len(block) == 3
        assert block[1]["match"] is True
        # Verify context lines have correct text content
        assert "before" in block[0]["text"]
        assert "ERROR" in block[1]["text"]
        assert "after" in block[2]["text"]
        # Non-match lines should be marked as not matching
        assert block[0]["match"] is False
        assert block[2]["match"] is False

    @respx.mock
    async def test_grep_invalid_regex(self, mock_ctx):
        build = make_build()
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/build-uuid-1").mock(
            return_value=httpx.Response(200, json=build)
        )
        respx.get(f"{build['log_url']}job-output.txt").mock(
            return_value=httpx.Response(200, text="some log content")
        )
        result = json.loads(await get_build_log(mock_ctx, "build-uuid-1", grep="[invalid"))
        assert "error" in result
        assert "regex" in result["error"].lower()

    @respx.mock
    async def test_custom_log_name(self, mock_ctx):
        build = make_build()
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/build-uuid-1").mock(
            return_value=httpx.Response(200, json=build)
        )
        custom_url = f"{build['log_url']}logs/controller/custom.log"
        respx.get(custom_url).mock(return_value=httpx.Response(200, text="custom log content"))
        result = json.loads(
            await get_build_log(mock_ctx, "build-uuid-1", log_name="logs/controller/custom.log")
        )
        assert result["total_lines"] == 1

    @respx.mock
    async def test_path_traversal_rejected(self, mock_ctx):
        build = make_build()
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/build-uuid-1").mock(
            return_value=httpx.Response(200, json=build)
        )
        result = json.loads(
            await get_build_log(mock_ctx, "build-uuid-1", log_name="../../etc/passwd")
        )
        assert "error" in result

    @respx.mock
    async def test_no_log_url_in_progress(self, mock_ctx):
        """In-progress build should return status-aware error."""
        build = make_build(log_url=None)
        build["log_url"] = None
        build["result"] = None
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/in-prog").mock(
            return_value=httpx.Response(200, json=build)
        )
        result = json.loads(await get_build_log(mock_ctx, "in-prog"))
        assert "error" in result
        assert "in progress" in result["error"]

    @respx.mock
    async def test_404_log_file(self, mock_ctx):
        build = make_build()
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/build-uuid-1").mock(
            return_value=httpx.Response(200, json=build)
        )
        respx.get(f"{build['log_url']}job-output.txt").mock(return_value=httpx.Response(404))
        result = json.loads(await get_build_log(mock_ctx, "build-uuid-1"))
        assert "error" in result
        assert "not found" in result["error"].lower()

    @respx.mock
    async def test_full_mode_pagination(self, mock_ctx):
        build = make_build()
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/build-uuid-1").mock(
            return_value=httpx.Response(200, json=build)
        )
        respx.get(f"{build['log_url']}job-output.txt").mock(
            return_value=httpx.Response(200, text=_SAMPLE_LOG)
        )
        result = json.loads(await get_build_log(mock_ctx, "build-uuid-1", mode="full", lines=50))
        assert result["offset"] == 50
        assert result["count"] == 150  # 200 lines - offset 50 = 150, capped at MAX_LOG_LINES=200

    @respx.mock
    async def test_start_line_beyond_total(self, mock_ctx):
        build = make_build()
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/build-uuid-1").mock(
            return_value=httpx.Response(200, json=build)
        )
        respx.get(f"{build['log_url']}job-output.txt").mock(
            return_value=httpx.Response(200, text="only one line")
        )
        result = json.loads(await get_build_log(mock_ctx, "build-uuid-1", start_line=999))
        assert "error" in result

    @respx.mock
    async def test_ansi_stripped(self, mock_ctx):
        build = make_build()
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/build-uuid-1").mock(
            return_value=httpx.Response(200, json=build)
        )
        respx.get(f"{build['log_url']}job-output.txt").mock(
            return_value=httpx.Response(200, text="\x1b[31mERROR\x1b[0m happened")
        )
        result = json.loads(await get_build_log(mock_ctx, "build-uuid-1"))
        # Verify ANSI codes are stripped from actual line text (not just JSON encoding)
        for line_obj in result.get("error_lines", []):
            assert "\x1b" not in line_obj["text"]
        for line_text in result.get("tail", []):
            assert "\x1b" not in line_text
        # Verify content is preserved after stripping
        assert any("ERROR" in t for t in result.get("tail", []))

    @respx.mock
    async def test_grep_context_deduplication(self, mock_ctx):
        """Adjacent matches should produce merged context blocks, not duplicates."""
        # Two matches on consecutive lines — their context blocks overlap
        log = "\n".join(
            [
                "line 1",
                "line 2",
                "line 3 ERROR first",
                "line 4 ERROR second",
                "line 5",
                "line 6",
            ]
        )
        build = make_build()
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/build-uuid-1").mock(
            return_value=httpx.Response(200, json=build)
        )
        respx.get(f"{build['log_url']}job-output.txt").mock(
            return_value=httpx.Response(200, content=log.encode())
        )
        result = json.loads(await get_build_log(mock_ctx, "build-uuid-1", grep="ERROR", context=2))
        assert result["matched"] == 2
        # Should be 1 merged block instead of 2 overlapping blocks
        assert len(result["blocks"]) == 1
        # The merged block should contain all lines from 1 to 6
        block_lines = [entry["n"] for entry in result["blocks"][0]]
        assert 1 in block_lines
        assert 6 in block_lines

    @respx.mock
    async def test_grep_context_non_overlapping(self, mock_ctx):
        """Distant matches should produce separate context blocks."""
        log = "\n".join([f"line {i}" for i in range(1, 21)])
        log = log.replace("line 3", "line 3 ERROR").replace("line 18", "line 18 ERROR")
        build = make_build()
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/build-uuid-1").mock(
            return_value=httpx.Response(200, json=build)
        )
        respx.get(f"{build['log_url']}job-output.txt").mock(
            return_value=httpx.Response(200, content=log.encode())
        )
        result = json.loads(await get_build_log(mock_ctx, "build-uuid-1", grep="ERROR", context=2))
        assert result["matched"] == 2
        # Should be 2 separate blocks (lines 1-5 and 16-20)
        assert len(result["blocks"]) == 2


class TestBrowseBuildLogs:
    @respx.mock
    async def test_no_log_url_in_progress(self, mock_ctx):
        """In-progress build should return status-aware error."""
        build = make_build(log_url=None)
        build["log_url"] = None
        build["result"] = None
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/in-prog").mock(
            return_value=httpx.Response(200, json=build)
        )
        result = json.loads(await browse_build_logs(mock_ctx, "in-prog"))
        assert "error" in result
        assert "in progress" in result["error"]

    @respx.mock
    async def test_directory_listing(self, mock_ctx):
        build = make_build()
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/build-uuid-1").mock(
            return_value=httpx.Response(200, json=build)
        )
        html = (
            '<html><body><a href="logs/">logs/</a><a href="zuul-info/">zuul-info/</a></body></html>'
        )
        respx.get(f"{build['log_url']}").mock(
            return_value=httpx.Response(200, text=html, headers={"content-type": "text/html"})
        )
        result = json.loads(await browse_build_logs(mock_ctx, "build-uuid-1"))
        assert "logs/" in result["entries"]
        assert "zuul-info/" in result["entries"]

    @respx.mock
    async def test_file_content(self, mock_ctx):
        build = make_build()
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/build-uuid-1").mock(
            return_value=httpx.Response(200, json=build)
        )
        respx.get(f"{build['log_url']}zuul-info/inventory.yaml").mock(
            return_value=httpx.Response(
                200,
                text="hosts:\n  controller:\n    ip: 10.0.0.1\n",
                headers={"content-type": "text/plain"},
            )
        )
        result = json.loads(
            await browse_build_logs(mock_ctx, "build-uuid-1", path="zuul-info/inventory.yaml")
        )
        assert "hosts:" in result["content"]
        assert result["truncated"] is False

    @respx.mock
    async def test_path_traversal_rejected(self, mock_ctx):
        build = make_build()
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/build-uuid-1").mock(
            return_value=httpx.Response(200, json=build)
        )
        result = json.loads(
            await browse_build_logs(mock_ctx, "build-uuid-1", path="../../etc/passwd")
        )
        assert "error" in result

    @respx.mock
    async def test_404(self, mock_ctx):
        build = make_build()
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/build-uuid-1").mock(
            return_value=httpx.Response(200, json=build)
        )
        respx.get(f"{build['log_url']}nonexistent/").mock(return_value=httpx.Response(404))
        result = json.loads(await browse_build_logs(mock_ctx, "build-uuid-1", path="nonexistent/"))
        assert "error" in result

    @respx.mock
    async def test_accepts_url(self, mock_ctx):
        build = make_build(uuid="browse-uuid")
        respx.get("https://zuul.example.com/api/tenant/my-tenant/build/browse-uuid").mock(
            return_value=httpx.Response(200, json=build)
        )
        html = '<html><body><a href="logs/">logs/</a></body></html>'
        respx.get(f"{build['log_url']}").mock(
            return_value=httpx.Response(200, text=html, headers={"content-type": "text/html"})
        )
        result = json.loads(
            await browse_build_logs(
                mock_ctx,
                url="https://zuul.example.com/t/my-tenant/build/browse-uuid",
            )
        )
        assert "logs/" in result["entries"]


class TestGetBuildLogUrl:
    @respx.mock
    async def test_accepts_url(self, mock_ctx):
        build = make_build(uuid="log-uuid")
        respx.get("https://zuul.example.com/api/tenant/my-tenant/build/log-uuid").mock(
            return_value=httpx.Response(200, json=build)
        )
        respx.get(f"{build['log_url']}job-output.txt").mock(
            return_value=httpx.Response(200, text="line 1\nFAILED! error\nline 3\n")
        )
        result = json.loads(
            await get_build_log(
                mock_ctx,
                url="https://zuul.example.com/t/my-tenant/build/log-uuid",
            )
        )
        assert result["total_lines"] == 3
        assert len(result["error_lines"]) >= 1


class TestTailBuildLog:
    @respx.mock
    async def test_returns_last_n_lines(self, mock_ctx):
        build = make_build()
        log_text = "\n".join([f"line {i}" for i in range(1, 101)])
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/build-uuid-1").mock(
            return_value=httpx.Response(200, json=build)
        )
        respx.get(f"{build['log_url']}job-output.txt").mock(
            return_value=httpx.Response(200, text=log_text)
        )
        result = json.loads(await tail_build_log(mock_ctx, uuid="build-uuid-1", lines=10))
        assert result["total_lines"] == 100
        assert result["count"] == 10
        assert result["tail_from"] == 91
        assert "line 100" in result["lines"][-1]
        assert result["job"] == "test-job"

    @respx.mock
    async def test_lines_clamped_to_500(self, mock_ctx):
        build = make_build()
        log_text = "\n".join([f"line {i}" for i in range(1, 11)])
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/build-uuid-1").mock(
            return_value=httpx.Response(200, json=build)
        )
        respx.get(f"{build['log_url']}job-output.txt").mock(
            return_value=httpx.Response(200, text=log_text)
        )
        # Request more lines than available — should return all
        result = json.loads(await tail_build_log(mock_ctx, uuid="build-uuid-1", lines=1000))
        assert result["count"] == 10
        assert result["tail_from"] == 1

    @respx.mock
    async def test_accepts_url(self, mock_ctx):
        build = make_build(uuid="tail-uuid")
        respx.get("https://zuul.example.com/api/tenant/my-tenant/build/tail-uuid").mock(
            return_value=httpx.Response(200, json=build)
        )
        respx.get(f"{build['log_url']}job-output.txt").mock(
            return_value=httpx.Response(200, text="last line\n")
        )
        result = json.loads(
            await tail_build_log(
                mock_ctx,
                url="https://zuul.example.com/t/my-tenant/build/tail-uuid",
            )
        )
        assert result["count"] == 1

    @respx.mock
    async def test_skip_postrun_finds_run_end_marker(self, mock_ctx):
        """With skip_postrun=True, tail should end at the RUN END marker."""
        build = make_build()
        # Simulate a log with run phase, RUN END marker, and post-run lines
        run_lines = [f"run line {i}" for i in range(1, 51)]
        marker = "2025-01-01 | RUN END RESULT_NORMAL"
        postrun_lines = [f"post-run collecting {i}" for i in range(1, 31)]
        log_text = "\n".join([*run_lines, marker, *postrun_lines])
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/build-uuid-1").mock(
            return_value=httpx.Response(200, json=build)
        )
        respx.get(f"{build['log_url']}job-output.txt").mock(
            return_value=httpx.Response(200, text=log_text)
        )
        result = json.loads(await tail_build_log(mock_ctx, uuid="build-uuid-1", lines=10))
        assert result["total_lines"] == 81  # 50 run + 1 marker + 30 postrun
        assert result["skipped_postrun"] is True
        assert result["postrun_lines"] == 30
        # Last line should be the RUN END marker, not postrun content
        assert "RUN END" in result["lines"][-1]
        # Should NOT contain postrun lines
        assert not any("post-run collecting" in line for line in result["lines"])

    @respx.mock
    async def test_skip_postrun_no_marker(self, mock_ctx):
        """Without a RUN END marker, skip_postrun should fall back to raw tail."""
        build = make_build()
        log_text = "\n".join([f"line {i}" for i in range(1, 101)])
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/build-uuid-1").mock(
            return_value=httpx.Response(200, json=build)
        )
        respx.get(f"{build['log_url']}job-output.txt").mock(
            return_value=httpx.Response(200, text=log_text)
        )
        result = json.loads(await tail_build_log(mock_ctx, uuid="build-uuid-1", lines=10))
        assert result["count"] == 10
        assert "skipped_postrun" not in result
        assert "line 100" in result["lines"][-1]

    @respx.mock
    async def test_skip_postrun_false_shows_raw_tail(self, mock_ctx):
        """With skip_postrun=False, should show the actual last lines including postrun."""
        build = make_build()
        run_lines = [f"run line {i}" for i in range(1, 51)]
        marker = "2025-01-01 | RUN END RESULT_NORMAL"
        postrun_lines = [f"post-run collecting {i}" for i in range(1, 31)]
        log_text = "\n".join([*run_lines, marker, *postrun_lines])
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/build-uuid-1").mock(
            return_value=httpx.Response(200, json=build)
        )
        respx.get(f"{build['log_url']}job-output.txt").mock(
            return_value=httpx.Response(200, text=log_text)
        )
        result = json.loads(
            await tail_build_log(
                mock_ctx, uuid="build-uuid-1", lines=10, skip_postrun=False,
            )
        )
        assert result["count"] == 10
        assert "skipped_postrun" not in result
        # Should contain postrun lines (raw tail from end)
        assert any("post-run collecting" in line for line in result["lines"])

    @respx.mock
    async def test_skip_postrun_only_applies_to_job_output_txt(self, mock_ctx):
        """skip_postrun should not apply to custom log files."""
        build = make_build()
        run_lines = [f"run line {i}" for i in range(1, 51)]
        marker = "2025-01-01 | RUN END RESULT_NORMAL"
        postrun_lines = [f"post-run collecting {i}" for i in range(1, 31)]
        log_text = "\n".join([*run_lines, marker, *postrun_lines])
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/build-uuid-1").mock(
            return_value=httpx.Response(200, json=build)
        )
        respx.get(f"{build['log_url']}logs/custom.log").mock(
            return_value=httpx.Response(200, text=log_text)
        )
        result = json.loads(
            await tail_build_log(
                mock_ctx, uuid="build-uuid-1", lines=10, log_name="logs/custom.log",
            )
        )
        assert "skipped_postrun" not in result
        # Should show raw tail — postrun lines at the end
        assert any("post-run collecting" in line for line in result["lines"])

    @respx.mock
    async def test_skip_postrun_short_log_shows_everything(self, mock_ctx):
        """When total lines <= requested lines, skip_postrun is bypassed."""
        build = make_build()
        log_text = "run line\n2025-01-01 | RUN END RESULT_NORMAL\npost-run line"
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/build-uuid-1").mock(
            return_value=httpx.Response(200, json=build)
        )
        respx.get(f"{build['log_url']}job-output.txt").mock(
            return_value=httpx.Response(200, text=log_text)
        )
        # Request 50 lines but log only has 3 — should show all
        result = json.loads(await tail_build_log(mock_ctx, uuid="build-uuid-1", lines=50))
        assert result["count"] == 3
        assert "skipped_postrun" not in result

    @respx.mock
    async def test_no_log_url_in_progress(self, mock_ctx):
        """In-progress build with no log_url should return status-aware error."""
        build = make_build(log_url=None)
        build["log_url"] = None
        build["result"] = None
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/in-prog").mock(
            return_value=httpx.Response(200, json=build)
        )
        result = json.loads(await tail_build_log(mock_ctx, uuid="in-prog"))
        assert "error" in result
        assert "in progress" in result["error"]

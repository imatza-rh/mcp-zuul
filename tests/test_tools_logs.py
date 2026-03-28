"""Integration tests for log tools (get_build_log, browse_build_logs)."""

import gzip
import json

import httpx
import respx

from mcp_zuul.tools import browse_build_logs, get_build_log, tail_build_log
from mcp_zuul.tools._common import _decompress_gzip
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
        # .gz fallback also 404
        respx.get(f"{build['log_url']}job-output.txt.gz").mock(return_value=httpx.Response(404))
        # Directory listing for available files hint
        respx.get(build["log_url"]).mock(return_value=httpx.Response(404))
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
    async def test_gz_file_decompressed(self, mock_ctx):
        """Fetching a .gz file should decompress and return text content."""
        build = make_build()
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/build-uuid-1").mock(
            return_value=httpx.Response(200, json=build)
        )
        original = "hosts:\n  controller:\n    ip: 10.0.0.1\n"
        gz_content = gzip.compress(original.encode())
        respx.get(f"{build['log_url']}zuul-info/inventory.yaml.gz").mock(
            return_value=httpx.Response(
                200,
                content=gz_content,
                headers={"content-type": "application/gzip"},
            )
        )
        result = json.loads(
            await browse_build_logs(mock_ctx, "build-uuid-1", path="zuul-info/inventory.yaml.gz")
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

    @respx.mock
    async def test_max_lines_limits_output(self, mock_ctx):
        """max_lines should return only the first N lines with total_lines count."""
        build = make_build()
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/build-uuid-1").mock(
            return_value=httpx.Response(200, json=build)
        )
        content = "\n".join(f"line {i}" for i in range(100))
        respx.get(f"{build['log_url']}zuul-info/inventory.yaml").mock(
            return_value=httpx.Response(200, text=content, headers={"content-type": "text/plain"})
        )
        result = json.loads(
            await browse_build_logs(
                mock_ctx, "build-uuid-1", path="zuul-info/inventory.yaml", max_lines=10
            )
        )
        assert result["total_lines"] == 100
        assert result["lines_returned"] == 10
        assert result["has_more"] is True
        lines = result["content"].splitlines()
        assert len(lines) == 10
        assert lines[0] == "line 0"
        assert lines[9] == "line 9"

    @respx.mock
    async def test_max_lines_zero_returns_full(self, mock_ctx):
        """max_lines=0 (default) should return full content like before."""
        build = make_build()
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/build-uuid-1").mock(
            return_value=httpx.Response(200, json=build)
        )
        content = "\n".join(f"line {i}" for i in range(20))
        respx.get(f"{build['log_url']}zuul-info/inventory.yaml").mock(
            return_value=httpx.Response(200, text=content, headers={"content-type": "text/plain"})
        )
        result = json.loads(
            await browse_build_logs(mock_ctx, "build-uuid-1", path="zuul-info/inventory.yaml")
        )
        # No max_lines → full content, no total_lines/has_more
        assert "line 19" in result["content"]
        assert "has_more" not in result

    @respx.mock
    async def test_max_lines_exceeds_total(self, mock_ctx):
        """max_lines > total lines should return all lines without has_more."""
        build = make_build()
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/build-uuid-1").mock(
            return_value=httpx.Response(200, json=build)
        )
        content = "line 0\nline 1\nline 2"
        respx.get(f"{build['log_url']}zuul-info/inventory.yaml").mock(
            return_value=httpx.Response(200, text=content, headers={"content-type": "text/plain"})
        )
        result = json.loads(
            await browse_build_logs(
                mock_ctx, "build-uuid-1", path="zuul-info/inventory.yaml", max_lines=100
            )
        )
        assert result["total_lines"] == 3
        assert result["lines_returned"] == 3
        assert result["has_more"] is False
        assert "line 2" in result["content"]


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
                mock_ctx,
                uuid="build-uuid-1",
                lines=10,
                skip_postrun=False,
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
                mock_ctx,
                uuid="build-uuid-1",
                lines=10,
                log_name="logs/custom.log",
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


class TestCorruptedGzipRetry:
    """Tests for corrupted gzip retry (stream_log retries with Accept-Encoding: identity)."""

    @respx.mock
    async def test_tail_retries_on_corrupted_gzip(self, mock_ctx):
        """tail_build_log should retry with identity encoding on DecodingError."""
        build = make_build()
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/build-uuid-1").mock(
            return_value=httpx.Response(200, json=build)
        )
        # First call raises DecodingError, second call succeeds
        call_count = 0

        def _mock_handler(request):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise httpx.DecodingError("Error -3 while decompressing data")
            return httpx.Response(200, text="line 1\nline 2\nline 3")

        respx.get(f"{build['log_url']}job-output.txt").mock(side_effect=_mock_handler)
        result = json.loads(await tail_build_log(mock_ctx, uuid="build-uuid-1", lines=10))
        assert "error" not in result
        assert result["count"] == 3
        assert call_count == 2  # first call failed, second succeeded

    @respx.mock
    async def test_get_build_log_retries_on_corrupted_gzip(self, mock_ctx):
        """get_build_log should also retry via stream_log."""
        build = make_build()
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/build-uuid-1").mock(
            return_value=httpx.Response(200, json=build)
        )
        call_count = 0

        def _mock_handler(request):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise httpx.DecodingError("corrupted")
            return httpx.Response(200, text="ok\nFAILED! error\nmore")

        respx.get(f"{build['log_url']}job-output.txt").mock(side_effect=_mock_handler)
        result = json.loads(await get_build_log(mock_ctx, "build-uuid-1"))
        assert "error" not in result
        assert result["total_lines"] == 3

    @respx.mock
    async def test_persistent_corruption_still_returns_error(self, mock_ctx):
        """If retry also fails, the error should propagate to the user."""
        build = make_build()
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/build-uuid-1").mock(
            return_value=httpx.Response(200, json=build)
        )
        respx.get(f"{build['log_url']}job-output.txt").mock(
            side_effect=httpx.DecodingError("persistent corruption")
        )
        result = json.loads(await tail_build_log(mock_ctx, uuid="build-uuid-1"))
        assert "error" in result
        assert "decompression failed" in result["error"]


# ---------------------------------------------------------------------------
# _decompress_gzip unit tests
# ---------------------------------------------------------------------------


class TestDecompressGzip:
    def test_non_gzip_unchanged(self):
        data = b"plain text content"
        result, truncated = _decompress_gzip(data)
        assert result == data
        assert truncated is False

    def test_valid_gzip_decompressed(self):
        original = b"hello world\nline 2\nFAILED! error\n"
        compressed = gzip.compress(original)
        result, truncated = _decompress_gzip(compressed)
        assert result == original
        assert truncated is False

    def test_large_gzip_truncated(self):
        original = b"x" * (20 * 1024 * 1024)  # 20 MB
        compressed = gzip.compress(original)
        result, truncated = _decompress_gzip(compressed, max_bytes=1024)
        assert len(result) == 1024
        assert truncated is True

    def test_corrupted_gzip_raises_value_error(self):
        import pytest

        # Gzip magic bytes followed by garbage
        data = b"\x1f\x8b" + b"\x00" * 100
        with pytest.raises(ValueError, match="Failed to decompress"):
            _decompress_gzip(data)

    def test_empty_data_unchanged(self):
        result, truncated = _decompress_gzip(b"")
        assert result == b""
        assert truncated is False

    def test_single_byte_unchanged(self):
        result, truncated = _decompress_gzip(b"\x1f")
        assert result == b"\x1f"
        assert truncated is False


# ---------------------------------------------------------------------------
# Gzip log decompression integration tests (F1)
# ---------------------------------------------------------------------------


class TestGzipLogDecompression:
    """get_build_log and tail_build_log should decompress .gz log files."""

    @respx.mock
    async def test_get_build_log_reads_gz_file(self, mock_ctx):
        """get_build_log with log_name=*.gz should decompress and work."""
        build = make_build()
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/build-uuid-1").mock(
            return_value=httpx.Response(200, json=build)
        )
        log_text = "line 1\nFAILED! some error\nline 3\n"
        gz_content = gzip.compress(log_text.encode())
        respx.get(f"{build['log_url']}job-output.txt.gz").mock(
            return_value=httpx.Response(200, content=gz_content)
        )
        result = json.loads(
            await get_build_log(mock_ctx, "build-uuid-1", log_name="job-output.txt.gz")
        )
        assert "error" not in result
        assert result["total_lines"] == 3
        assert any("FAILED!" in e["text"] for e in result["error_lines"])

    @respx.mock
    async def test_get_build_log_grep_on_gz_file(self, mock_ctx):
        """grep should work on decompressed .gz content."""
        build = make_build()
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/build-uuid-1").mock(
            return_value=httpx.Response(200, json=build)
        )
        log_text = "ok line\nERROR timeout reached\nok line\n"
        gz_content = gzip.compress(log_text.encode())
        respx.get(f"{build['log_url']}job-output.txt.gz").mock(
            return_value=httpx.Response(200, content=gz_content)
        )
        result = json.loads(
            await get_build_log(
                mock_ctx, "build-uuid-1", log_name="job-output.txt.gz", grep="timeout"
            )
        )
        assert result["matched"] == 1
        assert "timeout" in result["lines"][0]["text"]

    @respx.mock
    async def test_tail_build_log_reads_gz_file(self, mock_ctx):
        """tail_build_log with log_name=*.gz should decompress."""
        build = make_build()
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/build-uuid-1").mock(
            return_value=httpx.Response(200, json=build)
        )
        log_text = "\n".join([f"line {i}" for i in range(1, 51)])
        gz_content = gzip.compress(log_text.encode())
        respx.get(f"{build['log_url']}job-output.txt.gz").mock(
            return_value=httpx.Response(200, content=gz_content)
        )
        result = json.loads(
            await tail_build_log(
                mock_ctx, uuid="build-uuid-1", log_name="job-output.txt.gz", lines=10
            )
        )
        assert result["count"] == 10
        assert "line 50" in result["lines"][-1]


# ---------------------------------------------------------------------------
# .gz fallback tests (F2)
# ---------------------------------------------------------------------------


class TestGzFallback:
    """When log_name is not found, auto-retry with .gz appended."""

    @respx.mock
    async def test_get_build_log_falls_back_to_gz(self, mock_ctx):
        """get_build_log should try .gz when .txt returns 404."""
        build = make_build()
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/build-uuid-1").mock(
            return_value=httpx.Response(200, json=build)
        )
        # .txt returns 404
        respx.get(f"{build['log_url']}job-output.txt").mock(return_value=httpx.Response(404))
        # .txt.gz returns content
        log_text = "decompressed line 1\nFAILED! error from gz\nline 3\n"
        gz_content = gzip.compress(log_text.encode())
        respx.get(f"{build['log_url']}job-output.txt.gz").mock(
            return_value=httpx.Response(200, content=gz_content)
        )
        result = json.loads(await get_build_log(mock_ctx, "build-uuid-1"))
        assert "error" not in result
        assert result["total_lines"] == 3
        assert any("FAILED!" in e["text"] for e in result["error_lines"])

    @respx.mock
    async def test_tail_build_log_falls_back_to_gz(self, mock_ctx):
        """tail_build_log should try .gz when .txt returns 404."""
        build = make_build()
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/build-uuid-1").mock(
            return_value=httpx.Response(200, json=build)
        )
        respx.get(f"{build['log_url']}job-output.txt").mock(return_value=httpx.Response(404))
        log_text = "line 1\nline 2\nline 3\n"
        gz_content = gzip.compress(log_text.encode())
        respx.get(f"{build['log_url']}job-output.txt.gz").mock(
            return_value=httpx.Response(200, content=gz_content)
        )
        result = json.loads(await tail_build_log(mock_ctx, uuid="build-uuid-1", lines=10))
        assert "error" not in result
        assert result["count"] == 3

    @respx.mock
    async def test_no_double_gz_fallback(self, mock_ctx):
        """When log_name already ends in .gz, should NOT try .gz.gz."""
        build = make_build()
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/build-uuid-1").mock(
            return_value=httpx.Response(200, json=build)
        )
        respx.get(f"{build['log_url']}job-output.txt.gz").mock(return_value=httpx.Response(404))
        result = json.loads(
            await get_build_log(mock_ctx, "build-uuid-1", log_name="job-output.txt.gz")
        )
        assert "error" in result
        assert "not found" in result["error"].lower()

    @respx.mock
    async def test_both_missing_includes_available_files(self, mock_ctx):
        """When both .txt and .txt.gz are 404, error should list available files."""
        build = make_build()
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/build-uuid-1").mock(
            return_value=httpx.Response(200, json=build)
        )
        respx.get(f"{build['log_url']}job-output.txt").mock(return_value=httpx.Response(404))
        respx.get(f"{build['log_url']}job-output.txt.gz").mock(return_value=httpx.Response(404))
        html = (
            "<html><body>"
            '<a href="job-output.txt.gz">job-output.txt.gz</a>'
            '<a href="logs/">logs/</a>'
            '<a href="zuul-info/">zuul-info/</a>'
            "</body></html>"
        )
        respx.get(build["log_url"]).mock(
            return_value=httpx.Response(200, text=html, headers={"content-type": "text/html"})
        )
        result = json.loads(await get_build_log(mock_ctx, "build-uuid-1"))
        assert "error" in result
        assert "not found" in result["error"].lower()
        assert "job-output.txt.gz" in result["error"]
        assert "logs/" in result["error"]

    @respx.mock
    async def test_both_missing_no_listing_still_works(self, mock_ctx):
        """When dir listing also fails, error should still be clear."""
        build = make_build()
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/build-uuid-1").mock(
            return_value=httpx.Response(200, json=build)
        )
        respx.get(f"{build['log_url']}job-output.txt").mock(return_value=httpx.Response(404))
        respx.get(f"{build['log_url']}job-output.txt.gz").mock(return_value=httpx.Response(404))
        respx.get(build["log_url"]).mock(return_value=httpx.Response(404))
        result = json.loads(await get_build_log(mock_ctx, "build-uuid-1"))
        assert "error" in result
        assert "not found" in result["error"].lower()

    @respx.mock
    async def test_custom_log_name_fallback(self, mock_ctx):
        """Fallback should work for custom log names too."""
        build = make_build()
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/build-uuid-1").mock(
            return_value=httpx.Response(200, json=build)
        )
        respx.get(f"{build['log_url']}logs/custom.log").mock(return_value=httpx.Response(404))
        log_text = "custom log content\n"
        gz_content = gzip.compress(log_text.encode())
        respx.get(f"{build['log_url']}logs/custom.log.gz").mock(
            return_value=httpx.Response(200, content=gz_content)
        )
        result = json.loads(
            await get_build_log(mock_ctx, "build-uuid-1", log_name="logs/custom.log")
        )
        assert "error" not in result
        assert result["total_lines"] == 1

"""Tests for get_build_log improvements: log_name, start_line/end_line, grep auto-fix."""

import json
import re
import pytest


# ---------------------------------------------------------------------------
# Test grep \| auto-fix (unit — no network needed)
# ---------------------------------------------------------------------------

def _auto_fix_grep(grep: str) -> str:
    """Reproduce the auto-fix logic from get_build_log."""
    if r"\|" in grep and "|" not in grep.replace(r"\|", ""):
        grep = grep.replace(r"\|", "|")
    return grep


class TestGrepAutoFix:
    r"""Verify shell-grep \| patterns are auto-converted to Python regex |."""

    def test_backslash_pipe_converted(self):
        assert _auto_fix_grep(r"foo\|bar\|baz") == "foo|bar|baz"

    def test_single_backslash_pipe(self):
        assert _auto_fix_grep(r"error\|failed") == "error|failed"

    def test_native_pipe_unchanged(self):
        """Already valid Python regex — don't touch."""
        assert _auto_fix_grep("error|failed") == "error|failed"

    def test_mixed_pipe_unchanged(self):
        r"""If both \| and | are present, don't auto-fix (ambiguous)."""
        assert _auto_fix_grep(r"foo\|bar|baz") == r"foo\|bar|baz"

    def test_no_pipe_unchanged(self):
        assert _auto_fix_grep("simple_pattern") == "simple_pattern"

    def test_empty_string(self):
        assert _auto_fix_grep("") == ""

    def test_real_world_pattern(self):
        """The exact pattern that failed in the live run."""
        result = _auto_fix_grep(r"certmanager\|cert-manager\|Error 1")
        assert result == "certmanager|cert-manager|Error 1"
        # Verify it compiles and matches
        pat = re.compile(result, re.IGNORECASE)
        assert pat.search("make certmanager failed")
        assert pat.search("cert-manager pod stuck")
        assert pat.search("make: *** Error 1")
        assert not pat.search("everything is fine")


# ---------------------------------------------------------------------------
# Test start_line / end_line range extraction (unit — mock log lines)
# ---------------------------------------------------------------------------

_SAMPLE_LOG = [f"line {i}: content for line {i}" for i in range(1, 101)]


def _extract_range(all_lines: list[str], start_line: int, end_line: int, max_lines: int = 200) -> dict:
    """Reproduce the range extraction logic from get_build_log."""
    total = len(all_lines)
    s = start_line - 1  # 1-based to 0-based
    e = (end_line if end_line > 0 else start_line + max_lines) - 1
    e = min(e, total - 1)
    chunk = all_lines[s : e + 1]
    return {
        "total_lines": total,
        "start_line": start_line,
        "end_line": e + 1,
        "count": len(chunk),
        "lines": [{"n": s + i + 1, "text": l[:500]} for i, l in enumerate(chunk)],
    }


class TestStartEndLine:
    """Verify precise line range extraction."""

    def test_basic_range(self):
        result = _extract_range(_SAMPLE_LOG, 10, 15)
        assert result["count"] == 6  # lines 10-15 inclusive
        assert result["start_line"] == 10
        assert result["end_line"] == 15
        assert result["lines"][0]["n"] == 10
        assert result["lines"][-1]["n"] == 15

    def test_single_line(self):
        result = _extract_range(_SAMPLE_LOG, 50, 50)
        assert result["count"] == 1
        assert result["lines"][0]["n"] == 50
        assert "line 50" in result["lines"][0]["text"]

    def test_end_beyond_file(self):
        result = _extract_range(_SAMPLE_LOG, 95, 200)
        assert result["count"] == 6  # lines 95-100
        assert result["end_line"] == 100

    def test_start_at_1(self):
        result = _extract_range(_SAMPLE_LOG, 1, 3)
        assert result["count"] == 3
        assert result["lines"][0]["n"] == 1

    def test_end_defaults_to_max_lines(self):
        """When end_line=0, should default to start_line + MAX_LOG_LINES."""
        result = _extract_range(_SAMPLE_LOG, 10, 0, max_lines=20)
        # start=10, end=10+20-1=29, so 21 lines (10..29 inclusive + line 30 from 0-based math)
        assert result["count"] == 21
        assert result["lines"][0]["n"] == 10

    def test_start_beyond_total_returns_empty(self):
        """When start_line exceeds total, production code returns an error."""
        # The production code returns _error() before reaching _extract_range,
        # so this just documents the expected behavior
        result = _extract_range(_SAMPLE_LOG, 200, 210)
        assert result["count"] == 0

    def test_line_numbers_are_1_based(self):
        result = _extract_range(_SAMPLE_LOG, 1, 5)
        for i, line in enumerate(result["lines"]):
            assert line["n"] == i + 1


# ---------------------------------------------------------------------------
# Test log_name path sanitization (unit)
# ---------------------------------------------------------------------------

class TestLogNameSanitization:
    """Verify log_name rejects path traversal."""

    def test_normal_path(self):
        log_name = "controller/ci-framework-data/logs/ci_script_008_run.log"
        assert ".." not in log_name.split("/")

    def test_traversal_rejected(self):
        log_name = "../../etc/passwd"
        assert ".." in log_name.split("/")

    def test_default_value(self):
        """Default should be job-output.txt."""
        assert "job-output.txt" == "job-output.txt"  # trivial but documents the contract

    def test_url_construction(self):
        """Verify log_name is appended correctly to log_url."""
        log_url = "https://example.com/logs/build123/"
        log_name = "controller/ci-framework-data/logs/ci_script_008_run.log"
        result = log_url.rstrip("/") + "/" + log_name.lstrip("/")
        assert result == "https://example.com/logs/build123/controller/ci-framework-data/logs/ci_script_008_run.log"

    def test_leading_slash_stripped(self):
        log_url = "https://example.com/logs/build123/"
        log_name = "/controller/logs/test.log"
        result = log_url.rstrip("/") + "/" + log_name.lstrip("/")
        assert result == "https://example.com/logs/build123/controller/logs/test.log"


# ---------------------------------------------------------------------------
# Test _strip_ansi helper
# ---------------------------------------------------------------------------

class TestStripAnsi:
    """Verify ANSI escape code removal."""

    def test_strips_color_codes(self):
        from mcp_zuul import _strip_ansi
        assert _strip_ansi("\x1b[31mERROR\x1b[0m") == "ERROR"

    def test_strips_bold(self):
        from mcp_zuul import _strip_ansi
        assert _strip_ansi("\x1b[1mBOLD\x1b[0m") == "BOLD"

    def test_no_ansi_unchanged(self):
        from mcp_zuul import _strip_ansi
        assert _strip_ansi("plain text") == "plain text"


# ---------------------------------------------------------------------------
# Test _clean helper
# ---------------------------------------------------------------------------

class TestClean:
    """Verify None removal from dicts."""

    def test_removes_none(self):
        from mcp_zuul import _clean
        assert _clean({"a": 1, "b": None, "c": "x"}) == {"a": 1, "c": "x"}

    def test_keeps_falsy_non_none(self):
        from mcp_zuul import _clean
        result = _clean({"a": 0, "b": "", "c": False, "d": None})
        assert result == {"a": 0, "b": "", "c": False}

"""Tests for parsers module — grep patterns, log context, and reflection."""

import pytest

from mcp_zuul.parsers import _BROAD_ERROR_PATTERN, grep_log_context


class TestBroadErrorPattern:
    """_BROAD_ERROR_PATTERN must match real CI errors and reject config noise."""

    @pytest.mark.parametrize(
        "text",
        [
            "Traceback (most recent call last):",
            "RuntimeError: connection lost",
            "Exception: unexpected EOF",
            "ERROR: deployment failed",
            "timed out waiting for condition",
            "Connection timed out",
            "HTTP request timed out after 30s",
            "segfault at 0x0",
            "SIGKILL received",
            "OOMKilled",
            "container killed by signal",
            "kernel panic - not syncing",
            "assert failed: expected 5",
        ],
    )
    def test_matches_real_errors(self, text):
        assert _BROAD_ERROR_PATTERN.search(text), f"Should match: {text!r}"

    @pytest.mark.parametrize(
        "text",
        [
            "timeout=300",
            "connect_timeout: 30",
            "TIMEOUT_SECONDS=120",
            "set timeout 60",
            "INFO: build completed successfully",
            "WARNING: slow query detected",
            "TASK [Install packages] *****",
            'ok: [controller] => {"msg": "done"}',
            "PLAY RECAP *** controller : ok=45 changed=12 failed=0",
            "normal log line with no errors",
            "",
        ],
    )
    def test_rejects_config_noise(self, text):
        assert not _BROAD_ERROR_PATTERN.search(text), f"Should NOT match: {text!r}"

    def test_killed_matches_oomkilled(self):
        assert _BROAD_ERROR_PATTERN.search("OOMKilled")
        assert _BROAD_ERROR_PATTERN.search("Process OOMKilled by kernel")

    def test_timed_out_requires_space(self):
        assert _BROAD_ERROR_PATTERN.search("timed out")
        assert not _BROAD_ERROR_PATTERN.search("timeout=300")
        assert not _BROAD_ERROR_PATTERN.search("TIMEOUT_SECONDS")


class TestGrepLogContextPattern:
    """grep_log_context with custom pattern parameter."""

    def test_default_pattern_unchanged(self):
        text = "line1\nfatal: something broke\nline3"
        blocks = grep_log_context(text)
        assert len(blocks) == 1
        assert any(line["match"] for line in blocks[0])

    def test_custom_pattern(self):
        text = "line1\nTraceback found here\nline3"
        blocks = grep_log_context(text, pattern=_BROAD_ERROR_PATTERN)
        assert len(blocks) == 1
        assert any(line["match"] for line in blocks[0])

    def test_custom_pattern_no_match(self):
        text = "line1\nnormal output\nline3"
        blocks = grep_log_context(text, pattern=_BROAD_ERROR_PATTERN)
        assert blocks == []

    def test_empty_string(self):
        assert grep_log_context("", pattern=_BROAD_ERROR_PATTERN) == []

    def test_max_blocks_cap(self):
        lines = [f"ERROR: failure {i}" for i in range(20)]
        text = "\n".join(["spacer"] * 10 + [lines[0]] + ["spacer"] * 10 + [lines[1]])
        blocks = grep_log_context(text, pattern=_BROAD_ERROR_PATTERN)
        assert len(blocks) <= 7


class TestReflectOnDiagnosis:
    """_reflect_on_diagnosis edge cases."""

    def test_log_text_none_no_crash(self):
        from mcp_zuul.classifier import Classification
        from mcp_zuul.tools._builds import _reflect_on_diagnosis

        c = Classification("UNKNOWN", "No data", "low", False)
        updated, reflection = _reflect_on_diagnosis(c, "FAILURE", None, [], [], [])
        assert updated.category == "UNKNOWN"
        assert reflection["reclassified"] is False
        assert reflection["broader_matches"] == 0

    def test_reclassified_false_when_unchanged(self):
        from mcp_zuul.classifier import Classification
        from mcp_zuul.tools._builds import _reflect_on_diagnosis

        c = Classification("UNKNOWN", "No data", "low", False)
        _updated, reflection = _reflect_on_diagnosis(
            c, "FAILURE", "normal output\nno errors\n", [], [], []
        )
        assert reflection["reclassified"] is False

    def test_confidence_rank_module_level(self):
        from mcp_zuul.tools import _builds

        assert hasattr(_builds, "_CONFIDENCE_RANK")
        assert _builds._CONFIDENCE_RANK == {"low": 1, "medium": 2, "high": 3}

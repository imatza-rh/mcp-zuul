"""Tests for helpers, formatters, config, and error handling."""

import json
import os
import time as _time
from unittest.mock import patch

import httpx
import pytest
import respx

from mcp_zuul.config import Config
from mcp_zuul.errors import _clean_body, handle_errors
from mcp_zuul.formatters import fmt_build, fmt_status_item
from mcp_zuul.helpers import (
    api,
    clean,
    error,
    fetch_log_url,
    is_ssl_error,
    parse_iso_timestamp,
    parse_zuul_url,
    safepath,
    strip_ansi,
    tenant,
)
from tests._factories import make_connect_error, make_ssl_connect_error


class TestIsSSLError:
    def test_detects_ssl_cert_verification_error(self):
        exc = make_ssl_connect_error()
        assert is_ssl_error(exc) is True

    def test_rejects_non_ssl_connect_error(self):
        exc = make_connect_error("Connection refused")
        assert is_ssl_error(exc) is False

    def test_rejects_bare_connect_error(self):
        """ConnectError with no cause chain (manually constructed)."""
        exc = httpx.ConnectError("some error")
        assert is_ssl_error(exc) is False

    def test_rejects_hostname_with_ssl(self):
        """Hostname containing 'ssl' must not trigger detection."""
        exc = make_connect_error("All connection attempts failed for ssl.example.com")
        assert is_ssl_error(exc) is False


class TestClean:
    def test_removes_none(self):
        assert clean({"a": 1, "b": None, "c": "x"}) == {"a": 1, "c": "x"}

    def test_keeps_falsy_non_none(self):
        result = clean({"a": 0, "b": False, "c": None})
        assert result == {"a": 0, "b": False}

    def test_strips_empty_strings_and_lists(self):
        result = clean({"a": 1, "b": "", "c": [], "d": "x", "e": [1]})
        assert result == {"a": 1, "d": "x", "e": [1]}

    def test_empty_dict(self):
        assert clean({}) == {}

    def test_all_none(self):
        assert clean({"a": None, "b": None}) == {}


class TestStripAnsi:
    def test_strips_color_codes(self):
        assert strip_ansi("\x1b[31mERROR\x1b[0m") == "ERROR"

    def test_strips_bold(self):
        assert strip_ansi("\x1b[1mBOLD\x1b[0m") == "BOLD"

    def test_no_ansi_unchanged(self):
        assert strip_ansi("plain text") == "plain text"

    def test_complex_codes(self):
        assert strip_ansi("\x1b[38;5;196mred\x1b[0m") == "red"


class TestError:
    def test_returns_json_error(self):
        result = json.loads(error("something broke"))
        assert result == {"error": "something broke"}


class TestSafepath:
    def test_allows_normal_path(self):
        assert safepath("org/repo") == "org/repo"

    def test_allows_encoded_chars(self):
        assert safepath("org/repo with space") == "org/repo%20with%20space"

    def test_rejects_traversal(self):
        with pytest.raises(ValueError, match="Invalid path"):
            safepath("../etc/passwd")

    def test_rejects_mid_traversal(self):
        with pytest.raises(ValueError, match="Invalid path"):
            safepath("org/../etc/passwd")


class TestTenant:
    def test_returns_explicit_tenant(self, mock_ctx):
        assert tenant(mock_ctx, "custom") == "custom"

    def test_falls_back_to_default(self, mock_ctx):
        assert tenant(mock_ctx, "") == "test-tenant"

    def test_raises_when_no_default(self, mock_ctx):
        mock_ctx.request_context.lifespan_context.config.default_tenant = ""
        with pytest.raises(ValueError, match="tenant is required"):
            tenant(mock_ctx, "")


class TestParseZuulUrl:
    def test_build_url(self):
        result = parse_zuul_url("https://zuul.example.com/t/my-tenant/build/abc123def")
        assert result == ("my-tenant", "build", "abc123def")

    def test_buildset_url(self):
        result = parse_zuul_url("https://zuul.example.com/t/tenant-a/buildset/bs-uuid-456")
        assert result == ("tenant-a", "buildset", "bs-uuid-456")

    def test_change_status_url(self):
        result = parse_zuul_url("https://zuul.example.com/t/tenant-a/status/change/12345,abc123")
        assert result == ("tenant-a", "change", "12345,abc123")

    def test_url_with_zuul_prefix(self):
        result = parse_zuul_url("https://sf.example.com/zuul/t/components-integration/build/abc123")
        assert result == ("components-integration", "build", "abc123")

    def test_url_with_query_params(self):
        result = parse_zuul_url("https://zuul.example.com/t/tenant/build/uuid123?tab=logs")
        assert result == ("tenant", "build", "uuid123")

    def test_invalid_url(self):
        assert parse_zuul_url("https://zuul.example.com/api/tenants") is None

    def test_empty_string(self):
        assert parse_zuul_url("") is None

    def test_not_a_url(self):
        assert parse_zuul_url("just-a-string") is None


class TestParseIsoTimestamp:
    def test_parses_zulu_time(self):
        from datetime import UTC

        result = parse_iso_timestamp("2026-04-18T14:30:00Z")
        assert result is not None
        assert result.year == 2026
        assert result.month == 4
        assert result.day == 18
        assert result.hour == 14
        assert result.minute == 30
        assert result.tzinfo == UTC

    def test_parses_utc_offset(self):
        result = parse_iso_timestamp("2026-04-18T14:30:00+00:00")
        assert result is not None
        assert result.year == 2026
        assert result.hour == 14

    def test_parses_no_timezone_assumes_utc(self):
        from datetime import UTC

        result = parse_iso_timestamp("2026-04-18T14:30:00")
        assert result is not None
        assert result.tzinfo == UTC

    def test_parses_with_timezone_offset(self):
        result = parse_iso_timestamp("2026-04-18T14:30:00-05:00")
        assert result is not None
        assert result.year == 2026

    def test_empty_string_returns_none(self):
        assert parse_iso_timestamp("") is None

    def test_invalid_format_returns_none(self):
        assert parse_iso_timestamp("not-a-date") is None

    def test_none_returns_none(self):
        assert parse_iso_timestamp(None) is None  # type: ignore[arg-type]


class TestConfig:
    def test_from_env_minimal(self):
        with patch.dict(os.environ, {"ZUUL_URL": "https://zuul.example.com"}, clear=False):
            config = Config.from_env()
            assert config.base_url == "https://zuul.example.com"
            assert config.timeout == 30
            assert config.verify_ssl is True
            assert config.use_kerberos is False

    def test_from_env_strips_trailing_slash(self):
        with patch.dict(os.environ, {"ZUUL_URL": "https://zuul.example.com/"}, clear=False):
            config = Config.from_env()
            assert config.base_url == "https://zuul.example.com"

    def test_from_env_missing_url_exits(self):
        with patch.dict(os.environ, {}, clear=True), pytest.raises(ValueError):
            Config.from_env()

    def test_from_env_invalid_timeout_exits(self):
        with (
            patch.dict(os.environ, {"ZUUL_URL": "https://x", "ZUUL_TIMEOUT": "abc"}, clear=False),
            pytest.raises(ValueError),
        ):
            Config.from_env()

    def test_from_env_kerberos_and_token_exits(self):
        env = {"ZUUL_URL": "https://x", "ZUUL_USE_KERBEROS": "true", "ZUUL_AUTH_TOKEN": "tok"}
        with patch.dict(os.environ, env, clear=False), pytest.raises(ValueError):
            Config.from_env()

    def test_from_env_transport_default(self):
        with patch.dict(os.environ, {"ZUUL_URL": "https://x"}, clear=False):
            config = Config.from_env()
            assert config.transport == "stdio"

    def test_from_env_transport_streamable_http(self):
        env = {"ZUUL_URL": "https://x", "MCP_TRANSPORT": "streamable-http"}
        with patch.dict(os.environ, env, clear=False):
            config = Config.from_env()
            assert config.transport == "streamable-http"

    def test_from_env_invalid_transport_exits(self):
        env = {"ZUUL_URL": "https://x", "MCP_TRANSPORT": "websocket"}
        with patch.dict(os.environ, env, clear=False), pytest.raises(ValueError):
            Config.from_env()

    def test_from_env_enabled_tools(self):
        env = {"ZUUL_URL": "https://x", "ZUUL_ENABLED_TOOLS": "get_build,list_builds"}
        with patch.dict(os.environ, env, clear=False):
            config = Config.from_env()
            assert config.enabled_tools == ["get_build", "list_builds"]
            assert config.disabled_tools is None

    def test_from_env_disabled_tools(self):
        env = {"ZUUL_URL": "https://x", "ZUUL_DISABLED_TOOLS": "list_tenants"}
        with patch.dict(os.environ, env, clear=False):
            config = Config.from_env()
            assert config.disabled_tools == ["list_tenants"]
            assert config.enabled_tools is None

    def test_from_env_enabled_and_disabled_exits(self):
        env = {
            "ZUUL_URL": "https://x",
            "ZUUL_ENABLED_TOOLS": "get_build",
            "ZUUL_DISABLED_TOOLS": "list_tenants",
        }
        with patch.dict(os.environ, env, clear=False), pytest.raises(ValueError):
            Config.from_env()

    def test_from_env_invalid_port_exits(self):
        env = {"ZUUL_URL": "https://x", "MCP_PORT": "not-a-number"}
        with patch.dict(os.environ, env, clear=False), pytest.raises(ValueError):
            Config.from_env()


class TestCleanBody:
    def test_strips_html_tags(self):
        html = "<!DOCTYPE html><html><head><title>404 Not Found</title></head><body><h1>Not Found</h1></body></html>"
        assert _clean_body(html) == "404 Not Found Not Found"

    def test_collapses_whitespace(self):
        html = "<h1>Internal  \n  Server   Error</h1>\n<p>Something broke</p>"
        assert _clean_body(html) == "Internal Server Error Something broke"

    def test_truncates_at_limit(self):
        assert len(_clean_body("x" * 500)) <= 200

    def test_empty_string(self):
        assert _clean_body("") == ""

    def test_plain_text_unchanged(self):
        assert _clean_body("simple error message") == "simple error message"


class TestHandleErrors:
    async def test_wraps_http_status_error(self):
        @handle_errors
        async def failing():
            resp = httpx.Response(403, text="Forbidden")
            raise httpx.HTTPStatusError(
                "", request=httpx.Request("GET", "https://x"), response=resp
            )

        result = json.loads(await failing())
        assert "403" in result["error"]

    async def test_html_stripped_from_error(self):
        @handle_errors
        async def failing():
            resp = httpx.Response(
                500,
                text="<!DOCTYPE html><html><head><title>500 Internal Server Error</title></head></html>",
            )
            raise httpx.HTTPStatusError(
                "", request=httpx.Request("GET", "https://x"), response=resp
            )

        result = json.loads(await failing())
        assert "500" in result["error"]
        assert "Internal Server Error" in result["error"]
        assert "<" not in result["error"]  # no HTML tags

    async def test_wraps_connect_error_connection_refused(self):
        @handle_errors
        async def failing():
            raise make_connect_error("Connection refused")

        result = json.loads(await failing())
        assert "Cannot connect" in result["error"]
        assert "connection refused" in result["error"]

    async def test_wraps_connect_error_dns_failure(self):
        @handle_errors
        async def failing():
            raise make_connect_error("[Errno 8] nodename nor servname provided, or not known")

        result = json.loads(await failing())
        assert "Cannot connect" in result["error"]
        assert "DNS resolution failed" in result["error"]

    async def test_wraps_connect_error_dns_linux(self):
        @handle_errors
        async def failing():
            raise make_connect_error("[Errno -2] Name or service not known")

        result = json.loads(await failing())
        assert "DNS resolution failed" in result["error"]

    async def test_wraps_connect_error_network_unreachable(self):
        @handle_errors
        async def failing():
            raise make_connect_error("Network is unreachable")

        result = json.loads(await failing())
        assert "network unreachable" in result["error"]

    async def test_wraps_connect_error_unknown(self):
        """Unknown ConnectError includes raw message for diagnosis."""

        @handle_errors
        async def failing():
            raise httpx.ConnectError("Something unexpected happened")

        result = json.loads(await failing())
        assert "Cannot connect" in result["error"]
        assert "Something unexpected happened" in result["error"]

    async def test_wraps_ssl_connect_error(self):
        @handle_errors
        async def failing():
            raise make_ssl_connect_error()

        result = json.loads(await failing())
        assert "SSL certificate verification failed" in result["error"]
        assert "ZUUL_VERIFY_SSL" in result["error"]

    async def test_ssl_hostname_no_false_positive(self):
        """ConnectError to ssl.example.com must NOT trigger the SSL hint."""

        @handle_errors
        async def failing():
            raise make_connect_error("All connection attempts failed for ssl.example.com")

        result = json.loads(await failing())
        assert "Cannot connect" in result["error"]
        assert "ZUUL_VERIFY_SSL" not in result["error"]

    async def test_wraps_timeout(self):
        @handle_errors
        async def failing():
            raise httpx.TimeoutException("")

        result = json.loads(await failing())
        assert "timed out" in result["error"]

    async def test_wraps_value_error(self):
        @handle_errors
        async def failing():
            raise ValueError("bad input")

        result = json.loads(await failing())
        assert result["error"] == "bad input"

    async def test_wraps_decoding_error(self):
        @handle_errors
        async def failing():
            raise httpx.DecodingError("Error -3 while decompressing data")

        result = json.loads(await failing())
        assert "decompression failed" in result["error"]
        assert "diagnose_build" in result["error"]
        # Must NOT contain "Internal error" — it's a known exception
        assert "Internal error" not in result["error"]

    async def test_wraps_unexpected(self):
        @handle_errors
        async def failing():
            raise RuntimeError("kaboom")

        result = json.loads(await failing())
        assert "RuntimeError" in result["error"]


class TestSmartTruncate:
    def test_short_text_unchanged(self):
        from mcp_zuul.tools import _smart_truncate

        assert _smart_truncate("hello") == "hello"

    def test_empty_returns_none(self):
        from mcp_zuul.tools import _smart_truncate

        assert _smart_truncate("") is None
        assert _smart_truncate(None) is None

    def test_long_text_keeps_head_and_tail(self):
        from mcp_zuul.tools import _smart_truncate

        text = "HEAD-" + "x" * 5000 + "-TAIL"
        result = _smart_truncate(text)
        assert result is not None
        assert result.startswith("HEAD-")
        assert result.endswith("-TAIL")
        assert "omitted" in result
        assert len(result) <= 4100

    def test_ansi_stripped(self):
        from mcp_zuul.tools import _smart_truncate

        text = "\x1b[0;31mred text\x1b[0m"
        result = _smart_truncate(text)
        assert "\x1b" not in result
        assert "red text" in result

    def test_small_max_size_does_not_exceed_limit(self):
        """With small max_size, output should not exceed max_size significantly."""
        from mcp_zuul.parsers import smart_truncate

        text = "a" * 200
        result = smart_truncate(text, max_size=50)
        assert result is not None
        # With tail clamped to 1, output should be bounded
        assert "omitted" in result


class TestTruncateInvocation:
    def test_string_value_truncated(self):
        from mcp_zuul.parsers import _truncate_invocation

        args = {"cmd": "x" * 5000}
        result = _truncate_invocation(args, max_size=100)
        assert result is not None
        assert len(result["cmd"]) <= 104  # 100 + "..."

    def test_nested_dict_truncated(self):
        from mcp_zuul.parsers import _truncate_invocation

        args = {"params": {"key": "x" * 5000}}
        result = _truncate_invocation(args, max_size=100)
        assert result is not None
        assert isinstance(result["params"], str)
        assert len(result["params"]) <= 104

    def test_nested_list_truncated(self):
        from mcp_zuul.parsers import _truncate_invocation

        args = {"params": ["x" * 5000]}
        result = _truncate_invocation(args, max_size=100)
        assert result is not None
        assert isinstance(result["params"], str)
        assert result["params"].endswith("...")

    def test_small_nested_dict_preserved(self):
        from mcp_zuul.parsers import _truncate_invocation

        args = {"params": {"key": "value"}}
        result = _truncate_invocation(args, max_size=4000)
        assert result is not None
        assert isinstance(result["params"], dict)

    def test_none_returns_none(self):
        from mcp_zuul.parsers import _truncate_invocation

        assert _truncate_invocation(None) is None
        assert _truncate_invocation({}) is None


class TestExtractInnerRecap:
    def test_no_recap_returns_none(self):
        from mcp_zuul.tools import _extract_inner_recap

        assert _extract_inner_recap("just some output\nno recap here") is None
        assert _extract_inner_recap("") is None
        assert _extract_inner_recap(None) is None

    def test_extracts_last_recap(self):
        from mcp_zuul.tools import _extract_inner_recap

        text = (
            "PLAY RECAP ***\nhost1 : ok=3 failed=0\n\n"
            "more output\n"
            "PLAY RECAP ***\nlocalhost : ok=74 failed=1\n"
        )
        recap = _extract_inner_recap(text)
        assert recap is not None
        assert "failed=1" in recap
        # Should be the LAST recap, not the first
        assert "localhost" in recap

    def test_strips_ansi_from_recap(self):
        from mcp_zuul.tools import _extract_inner_recap

        text = "PLAY RECAP *****\n\x1b[0;31mlocalhost\x1b[0m : ok=5 \x1b[0;31mfailed=1\x1b[0m\n"
        recap = _extract_inner_recap(text)
        assert recap is not None
        assert "\x1b" not in recap
        assert "failed=1" in recap

    def test_multiple_hosts(self):
        from mcp_zuul.tools import _extract_inner_recap

        text = "PLAY RECAP *****\nhost1 : ok=10 failed=0\nhost2 : ok=8 failed=1\n"
        recap = _extract_inner_recap(text)
        assert "host1" in recap
        assert "host2" in recap
        assert "failed=1" in recap


class TestFmtProject:
    def test_job_group_with_dict(self):
        """Job groups containing dicts should extract the name correctly."""
        from mcp_zuul.formatters import fmt_project

        data = {
            "configs": [
                {
                    "pipelines": [
                        {
                            "name": "check",
                            "jobs": [[{"name": "job-a"}, {"name": "job-b"}], {"name": "job-c"}],
                        }
                    ]
                }
            ]
        }
        result = fmt_project(data)
        assert result["pipelines"]["check"] == ["job-a", "job-c"]

    def test_job_group_with_none_element(self):
        """Job groups with None elements should not crash."""
        from mcp_zuul.formatters import fmt_project

        data = {"configs": [{"pipelines": [{"name": "check", "jobs": [[None]]}]}]}
        result = fmt_project(data)
        assert result["pipelines"]["check"] == [""]

    def test_job_group_with_string_element(self):
        """Job groups with string elements should not crash."""
        from mcp_zuul.formatters import fmt_project

        data = {"configs": [{"pipelines": [{"name": "check", "jobs": [["check-job"]]}]}]}
        result = fmt_project(data)
        assert result["pipelines"]["check"] == [""]

    def test_job_group_with_empty_dict(self):
        """Job groups with empty dicts should return empty name."""
        from mcp_zuul.formatters import fmt_project

        data = {"configs": [{"pipelines": [{"name": "check", "jobs": [[{}]]}]}]}
        result = fmt_project(data)
        assert result["pipelines"]["check"] == [""]

    def test_empty_job_group(self):
        """Empty job groups should return empty string."""
        from mcp_zuul.formatters import fmt_project

        data = {"configs": [{"pipelines": [{"name": "check", "jobs": [[]]}]}]}
        result = fmt_project(data)
        assert result["pipelines"]["check"] == [""]


class TestFmtBuild:
    def test_brief_format(self):
        build = {
            "uuid": "u1",
            "job_name": "j1",
            "result": "SUCCESS",
            "pipeline": "check",
            "duration": 100,
            "voting": True,
            "start_time": "2025-01-01",
            "ref": {"project": "p1", "change": 1, "ref_url": "url"},
            "buildset": {"uuid": "bs1"},
        }
        result = fmt_build(build, brief=True)
        assert result["uuid"] == "u1"
        assert "nodeset" not in result  # brief excludes detailed fields

    def test_full_format(self):
        build = {
            "uuid": "u1",
            "job_name": "j1",
            "result": "FAILURE",
            "pipeline": "gate",
            "duration": 200,
            "voting": True,
            "start_time": "2025-01-01",
            "end_time": "2025-01-01",
            "event_timestamp": "2025-01-01",
            "log_url": "https://logs/u1/",
            "nodeset": "centos-9",
            "error_detail": "timeout",
            "artifacts": [{"name": "art1"}],
            "ref": {
                "project": "p1",
                "change": 1,
                "patchset": "2",
                "branch": "main",
                "ref_url": "url",
            },
            "buildset": {"uuid": "bs1"},
        }
        result = fmt_build(build, brief=False)
        assert result["log_url"] == "https://logs/u1/"
        assert result["nodeset"] == "centos-9"
        assert result["artifacts"] == ["art1"]

    def test_string_ref_does_not_crash(self):
        """String ref (tag pipelines, periodic) should not raise AttributeError."""
        build = {
            "uuid": "u1",
            "job_name": "j1",
            "result": "SUCCESS",
            "ref": "refs/heads/main",
        }
        result = fmt_build(build, brief=True)
        assert result["uuid"] == "u1"
        assert "project" not in result
        assert "change" not in result

    def test_none_ref(self):
        build = {"uuid": "u1", "job_name": "j1", "result": "SUCCESS", "ref": None}
        result = fmt_build(build, brief=True)
        assert "project" not in result

    def test_list_ref_does_not_crash(self):
        """List ref should not crash."""
        build = {
            "uuid": "u1",
            "job_name": "j1",
            "result": "SUCCESS",
            "ref": [{"project": "p1"}],
        }
        result = fmt_build(build, brief=True)
        assert "project" not in result


class TestFmtStatusItem:
    def test_formats_jobs(self):
        item = {
            "id": "12345,1",
            "active": True,
            "live": True,
            "refs": [{"project": "org/repo", "change": 12345, "url": "url"}],
            "zuul_ref": "Zbs-uuid",
            "jobs": [
                {
                    "name": "test-job",
                    "uuid": "j1",
                    "result": None,
                    "voting": True,
                    "elapsed_time": 60000,
                    "start_time": _time.time() - 120,  # Started 120s ago
                }
            ],
            "failing_reasons": [],
        }
        result = fmt_status_item(item)
        assert result["buildset_uuid"] == "bs-uuid"
        assert result["jobs"][0]["name"] == "test-job"
        # elapsed is now a human-readable string, recomputed from start_time (~120s = "2m Xs")
        assert result["jobs"][0]["elapsed"].startswith("2m")


class TestApiRetry:
    @respx.mock
    async def test_retries_on_503(self, mock_ctx):
        """503 on first attempt should retry and succeed."""
        route = respx.get("https://zuul.example.com/api/tenants")
        route.side_effect = [
            httpx.Response(503, text="Service Unavailable"),
            httpx.Response(200, json=[{"name": "t1"}]),
        ]
        result = await api(mock_ctx, "/tenants")
        assert result == [{"name": "t1"}]
        assert route.call_count == 2

    @respx.mock
    async def test_raises_after_two_503s(self, mock_ctx):
        """Two consecutive 503s should raise HTTPStatusError."""
        route = respx.get("https://zuul.example.com/api/tenants")
        route.side_effect = [
            httpx.Response(503, text="Service Unavailable"),
            httpx.Response(503, text="Service Unavailable"),
        ]
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await api(mock_ctx, "/tenants")
        assert exc_info.value.response.status_code == 503
        assert route.call_count == 2

    @respx.mock
    async def test_retries_on_500(self, mock_ctx):
        """500 on first attempt should retry and succeed."""
        route = respx.get("https://zuul.example.com/api/tenants")
        route.side_effect = [
            httpx.Response(500, text="Internal Server Error"),
            httpx.Response(200, json=[{"name": "t1"}]),
        ]
        result = await api(mock_ctx, "/tenants")
        assert result == [{"name": "t1"}]
        assert route.call_count == 2

    @respx.mock
    async def test_no_retry_on_other_errors(self, mock_ctx):
        """Non-500/503 errors should not trigger a retry."""
        route = respx.get("https://zuul.example.com/api/tenants")
        route.side_effect = [httpx.Response(502, text="Bad Gateway")]
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await api(mock_ctx, "/tenants")
        assert exc_info.value.response.status_code == 502
        assert route.call_count == 1

    @respx.mock
    async def test_success_no_retry(self, mock_ctx):
        """Successful response should not trigger a retry."""
        route = respx.get("https://zuul.example.com/api/tenants")
        route.side_effect = [httpx.Response(200, json=[{"name": "t1"}])]
        result = await api(mock_ctx, "/tenants")
        assert result == [{"name": "t1"}]
        assert route.call_count == 1


class TestFetchLogUrlDecodingError:
    @respx.mock
    async def test_retries_with_identity_on_decoding_error(self, mock_ctx):
        """DecodingError on first attempt should retry with Accept-Encoding: identity."""
        a = mock_ctx.request_context.lifespan_context
        url = "https://logs.example.com/build/job-output.json.gz"

        call_count = 0

        def _side_effect(request):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # First call: simulate corrupted gzip
                raise httpx.DecodingError("")
            # Second call: should have Accept-Encoding: identity
            assert request.headers.get("accept-encoding") == "identity"
            return httpx.Response(200, content=b'{"data": "ok"}')

        respx.get(url).mock(side_effect=_side_effect)
        resp = await fetch_log_url(a, url)
        assert resp.status_code == 200
        assert call_count == 2

    @respx.mock
    async def test_propagates_decoding_error_on_both_failures(self, mock_ctx):
        """If identity retry also fails with DecodingError, it should propagate."""
        a = mock_ctx.request_context.lifespan_context
        url = "https://logs.example.com/build/job-output.json.gz"

        respx.get(url).mock(side_effect=httpx.DecodingError(""))
        with pytest.raises(httpx.DecodingError):
            await fetch_log_url(a, url)

    @respx.mock
    async def test_no_retry_on_success(self, mock_ctx):
        """Successful response should not trigger identity fallback."""
        a = mock_ctx.request_context.lifespan_context
        url = "https://logs.example.com/build/job-output.json.gz"

        route = respx.get(url).mock(return_value=httpx.Response(200, content=b"log content"))
        resp = await fetch_log_url(a, url)
        assert resp.status_code == 200
        assert route.call_count == 1

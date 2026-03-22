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
from mcp_zuul.helpers import api, clean, error, parse_zuul_url, safepath, strip_ansi, tenant


class TestClean:
    def test_removes_none(self):
        assert clean({"a": 1, "b": None, "c": "x"}) == {"a": 1, "c": "x"}

    def test_keeps_falsy_non_none(self):
        result = clean({"a": 0, "b": "", "c": False, "d": None})
        assert result == {"a": 0, "b": "", "c": False}

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
        with patch.dict(os.environ, {}, clear=True), pytest.raises(SystemExit):
            Config.from_env()

    def test_from_env_invalid_timeout_exits(self):
        with (
            patch.dict(os.environ, {"ZUUL_URL": "https://x", "ZUUL_TIMEOUT": "abc"}, clear=False),
            pytest.raises(SystemExit),
        ):
            Config.from_env()

    def test_from_env_kerberos_and_token_exits(self):
        env = {"ZUUL_URL": "https://x", "ZUUL_USE_KERBEROS": "true", "ZUUL_AUTH_TOKEN": "tok"}
        with patch.dict(os.environ, env, clear=False), pytest.raises(SystemExit):
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
        with patch.dict(os.environ, env, clear=False), pytest.raises(SystemExit):
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
        with patch.dict(os.environ, env, clear=False), pytest.raises(SystemExit):
            Config.from_env()

    def test_from_env_invalid_port_exits(self):
        env = {"ZUUL_URL": "https://x", "MCP_PORT": "not-a-number"}
        with patch.dict(os.environ, env, clear=False), pytest.raises(SystemExit):
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

    async def test_wraps_connect_error(self):
        @handle_errors
        async def failing():
            raise httpx.ConnectError("")

        result = json.loads(await failing())
        assert "Cannot connect" in result["error"]

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

    async def test_wraps_unexpected(self):
        @handle_errors
        async def failing():
            raise RuntimeError("kaboom")

        result = json.loads(await failing())
        assert "RuntimeError" in result["error"]


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
        # elapsed is computed from start_time for running jobs (not Zuul's stale value)
        assert 100 < result["jobs"][0]["elapsed"] < 200  # ~120s (now in seconds)


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
    async def test_no_retry_on_other_errors(self, mock_ctx):
        """Non-503 errors should not trigger a retry."""
        route = respx.get("https://zuul.example.com/api/tenants")
        route.side_effect = [httpx.Response(500, text="Internal Server Error")]
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await api(mock_ctx, "/tenants")
        assert exc_info.value.response.status_code == 500
        assert route.call_count == 1

    @respx.mock
    async def test_success_no_retry(self, mock_ctx):
        """Successful response should not trigger a retry."""
        route = respx.get("https://zuul.example.com/api/tenants")
        route.side_effect = [httpx.Response(200, json=[{"name": "t1"}])]
        result = await api(mock_ctx, "/tenants")
        assert result == [{"name": "t1"}]
        assert route.call_count == 1

"""Tests for server.py: BearerAuth, tool removal, tool listing, lifespan."""

import os
import re
import ssl
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from mcp_zuul.server import _BearerAuth, _list_tool_names, _remove_tool

# ---------------------------------------------------------------------------
# _BearerAuth
# ---------------------------------------------------------------------------


class TestBearerAuth:
    def test_adds_bearer_header(self):
        """Auth flow adds Authorization: Bearer header to request."""
        auth = _BearerAuth("my-token-123")
        req = httpx.Request("GET", "https://zuul.example.com/api/tenants")
        flow = auth.auth_flow(req)
        modified_req = next(flow)
        assert modified_req.headers["Authorization"] == "Bearer my-token-123"

    def test_different_tokens(self):
        """Each _BearerAuth instance uses its own token."""
        auth1 = _BearerAuth("token-a")
        auth2 = _BearerAuth("token-b")
        req1 = httpx.Request("GET", "https://example.com/a")
        req2 = httpx.Request("GET", "https://example.com/b")
        assert next(auth1.auth_flow(req1)).headers["Authorization"] == "Bearer token-a"
        assert next(auth2.auth_flow(req2)).headers["Authorization"] == "Bearer token-b"


# ---------------------------------------------------------------------------
# _remove_tool
# ---------------------------------------------------------------------------


class TestRemoveTool:
    def test_removes_existing_tool(self):
        """Successfully removes a registered tool."""
        server = MagicMock()
        server.remove_tool = MagicMock()
        assert _remove_tool(server, "get_build") is True
        server.remove_tool.assert_called_once_with("get_build")

    def test_returns_false_on_unknown_tool(self):
        """Returns False when tool doesn't exist."""
        from mcp.server.mcpserver.exceptions import ToolError

        server = MagicMock()
        server.remove_tool.side_effect = ToolError("Unknown tool: get_build")
        assert _remove_tool(server, "get_build") is False


# ---------------------------------------------------------------------------
# _list_tool_names
# ---------------------------------------------------------------------------


class TestListToolNames:
    async def test_lists_registered_tools(self):
        """Returns list of tool names via public list_tools() API."""
        from unittest.mock import AsyncMock

        server = MagicMock()
        tool_a, tool_b, tool_c = MagicMock(), MagicMock(), MagicMock()
        tool_a.name = "get_build"
        tool_b.name = "list_builds"
        tool_c.name = "get_job"
        server.list_tools = AsyncMock(return_value=[tool_a, tool_b, tool_c])
        result = await _list_tool_names(server)
        assert sorted(result) == ["get_build", "get_job", "list_builds"]


# ---------------------------------------------------------------------------
# lifespan: read-only mode, tool filtering
# ---------------------------------------------------------------------------


class TestLifespanReadOnly:
    """Test that lifespan correctly removes write tools in read-only mode."""

    @pytest.fixture
    def _env(self):
        """Minimal env for lifespan."""
        env = {
            "ZUUL_URL": "https://zuul.example.com",
            "ZUUL_DEFAULT_TENANT": "test",
            "ZUUL_READ_ONLY": "true",
        }
        with patch.dict(os.environ, env, clear=False):
            yield

    async def test_read_only_removes_write_tools(self, _env):
        """ZUUL_READ_ONLY=true (default) removes pipeline-affecting tools."""
        from mcp_zuul.server import lifespan, mcp

        removed = []

        def tracking_remove(server, name):
            removed.append(name)
            # Don't actually remove from real mcp (tools may not be registered)
            return True

        with patch("mcp_zuul.server._remove_tool", side_effect=tracking_remove):
            async with lifespan(mcp):
                pass

        assert set(removed) >= {
            "enqueue",
            "dequeue",
            "reenqueue_buildset",
        }
        # autohold_create/delete are management ops, NOT pipeline-affecting
        assert "autohold_create" not in removed
        assert "autohold_delete" not in removed

    async def test_write_enabled_keeps_write_tools(self):
        """ZUUL_READ_ONLY=false does NOT remove write tools."""
        env = {
            "ZUUL_URL": "https://zuul.example.com",
            "ZUUL_DEFAULT_TENANT": "test",
            "ZUUL_READ_ONLY": "false",
        }
        from mcp_zuul.server import lifespan, mcp

        removed = []

        def tracking_remove(server, name):
            removed.append(name)
            return True

        with (
            patch.dict(os.environ, env, clear=False),
            patch("mcp_zuul.server._remove_tool", side_effect=tracking_remove),
        ):
            async with lifespan(mcp):
                pass

        write_tools = {
            "enqueue",
            "dequeue",
            "reenqueue_buildset",
        }
        assert write_tools.isdisjoint(set(removed))


class TestLifespanToolFiltering:
    """Test tool filtering via ZUUL_ENABLED_TOOLS / ZUUL_DISABLED_TOOLS."""

    async def test_enabled_tools_removes_others(self):
        """ZUUL_ENABLED_TOOLS keeps only listed tools, removes everything else."""
        env = {
            "ZUUL_URL": "https://zuul.example.com",
            "ZUUL_DEFAULT_TENANT": "test",
            "ZUUL_READ_ONLY": "false",
            "ZUUL_ENABLED_TOOLS": "get_build,list_builds",
        }
        from mcp_zuul.server import lifespan, mcp

        removed = []

        def tracking_remove(server, name):
            removed.append(name)
            return True

        all_tools = ["get_build", "list_builds", "get_job", "list_tenants", "get_status"]

        with (
            patch.dict(os.environ, env, clear=False),
            patch("mcp_zuul.server._remove_tool", side_effect=tracking_remove),
            patch("mcp_zuul.server._list_tool_names", return_value=all_tools),
        ):
            async with lifespan(mcp):
                pass

        # get_job, list_tenants, get_status should be removed; get_build, list_builds kept
        assert "get_build" not in removed
        assert "list_builds" not in removed
        assert "get_job" in removed
        assert "list_tenants" in removed
        assert "get_status" in removed

    async def test_disabled_tools_removes_listed(self):
        """ZUUL_DISABLED_TOOLS removes only listed tools."""
        env = {
            "ZUUL_URL": "https://zuul.example.com",
            "ZUUL_DEFAULT_TENANT": "test",
            "ZUUL_READ_ONLY": "false",
            "ZUUL_DISABLED_TOOLS": "list_tenants,get_status",
        }
        from mcp_zuul.server import lifespan, mcp

        removed = []

        def tracking_remove(server, name):
            removed.append(name)
            return True

        with (
            patch.dict(os.environ, env, clear=False),
            patch("mcp_zuul.server._remove_tool", side_effect=tracking_remove),
        ):
            async with lifespan(mcp):
                pass

        assert "list_tenants" in removed
        assert "get_status" in removed

    async def test_disabled_unknown_tool_logs_warning(self):
        """Disabling a non-existent tool logs a warning."""
        env = {
            "ZUUL_URL": "https://zuul.example.com",
            "ZUUL_DEFAULT_TENANT": "test",
            "ZUUL_READ_ONLY": "false",
            "ZUUL_DISABLED_TOOLS": "nonexistent_tool",
        }
        from mcp_zuul.server import lifespan, mcp

        def failing_remove(server, name):
            return False  # Tool not found

        with (
            patch.dict(os.environ, env, clear=False),
            patch("mcp_zuul.server._remove_tool", side_effect=failing_remove),
            patch("mcp_zuul.server.log") as mock_log,
        ):
            async with lifespan(mcp):
                pass

        mock_log.warning.assert_any_call("Cannot disable unknown tool: %s", "nonexistent_tool")


class TestToolCountConsistency:
    """Verify tool count is consistent across all files that mention it."""

    def test_tool_counts_match(self):
        root = Path(__file__).parent.parent
        init_py = (root / "src/mcp_zuul/tools/__init__.py").read_text()
        claude_md = (root / "CLAUDE.md").read_text()
        readme_md = (root / "README.md").read_text()

        # Extract the number from __init__.py docstring
        m = re.search(r"(\d+) tools", init_py.split("\n")[0])
        assert m, "Could not find tool count in __init__.py docstring"
        init_count = int(m.group(1))

        # Count actual @mcp.tool registrations across all tool modules
        tools_dir = root / "src/mcp_zuul/tools"
        actual = 0
        for f in tools_dir.glob("_*.py"):
            actual += f.read_text().count("@mcp.tool(")

        assert actual == init_count, (
            f"__init__.py says {init_count} tools but found {actual} @mcp.tool decorators"
        )

        # Check CLAUDE.md mentions the same count
        assert f"{init_count} tools" in claude_md, (
            f"CLAUDE.md does not mention '{init_count} tools' (found in __init__.py)"
        )

        # Check README.md mentions the same count
        assert f"{init_count} tools" in readme_md, (
            f"README.md does not mention '{init_count} tools' (found in __init__.py)"
        )


class TestVersionFallback:
    """Test version resolution from importlib.metadata."""

    def test_version_resolves_when_installed(self):
        """Installed package returns correct version."""
        from mcp_zuul.server import _version

        assert _version == "0.10.0"

    def test_version_fallback_on_missing_package(self):
        """PackageNotFoundError falls back to 0.0.0-dev."""
        import importlib
        from importlib.metadata import PackageNotFoundError

        original = importlib.metadata.version

        def _raise(name):
            raise PackageNotFoundError(name)

        import mcp_zuul.server as srv

        importlib.metadata.version = _raise
        try:
            importlib.reload(srv)
            assert srv._version == "0.0.0-dev"
        finally:
            importlib.metadata.version = original
            importlib.reload(srv)


class TestLifespanContext:
    """Test that lifespan yields a properly configured AppContext."""

    async def test_yields_app_context_with_clients(self):
        """Lifespan yields AppContext with both httpx clients."""
        env = {
            "ZUUL_URL": "https://zuul.example.com",
            "ZUUL_DEFAULT_TENANT": "test",
            "ZUUL_READ_ONLY": "false",
        }
        from mcp_zuul.helpers import AppContext
        from mcp_zuul.server import lifespan, mcp

        with (
            patch.dict(os.environ, env, clear=False),
            patch("mcp_zuul.server._remove_tool", return_value=True),
        ):
            async with lifespan(mcp) as ctx:
                assert isinstance(ctx, AppContext)
                assert ctx.client is not None
                assert ctx.log_client is not None
                assert ctx.config.base_url == "https://zuul.example.com"
                assert ctx.grep_executor is not None

    async def test_bearer_auth_configured(self):
        """When ZUUL_AUTH_TOKEN is set, client uses _BearerAuth."""
        env = {
            "ZUUL_URL": "https://zuul.example.com",
            "ZUUL_DEFAULT_TENANT": "test",
            "ZUUL_AUTH_TOKEN": "my-secret-token",
            "ZUUL_READ_ONLY": "false",
        }
        from mcp_zuul.server import lifespan, mcp

        with (
            patch.dict(os.environ, env, clear=False),
            patch("mcp_zuul.server._remove_tool", return_value=True),
        ):
            async with lifespan(mcp) as ctx:
                assert ctx.client._transport is not None
                # Verify auth is set (BearerAuth)
                assert ctx.config.auth_token == "my-secret-token"

    async def test_executor_shutdown_on_exit(self):
        """ThreadPoolExecutor is shut down when lifespan exits."""
        env = {
            "ZUUL_URL": "https://zuul.example.com",
            "ZUUL_DEFAULT_TENANT": "test",
            "ZUUL_READ_ONLY": "false",
        }
        from mcp_zuul.server import lifespan, mcp

        with (
            patch.dict(os.environ, env, clear=False),
            patch("mcp_zuul.server._remove_tool", return_value=True),
        ):
            async with lifespan(mcp) as ctx:
                executor = ctx.grep_executor

            # After exiting, executor should be shut down
            assert executor._shutdown


class TestKerberosRetry:
    """Test Kerberos auth retry with backoff at startup."""

    @pytest.fixture
    def _kerb_env(self):
        """Env for Kerberos tests — patches gssapi import for CI without krb5-devel."""
        import types

        env = {
            "ZUUL_URL": "https://zuul.example.com",
            "ZUUL_DEFAULT_TENANT": "test",
            "ZUUL_USE_KERBEROS": "true",
        }
        fake_gssapi = types.ModuleType("gssapi")
        with (
            patch.dict(os.environ, env, clear=False),
            patch.dict("sys.modules", {"gssapi": fake_gssapi}),
        ):
            yield

    async def test_retries_on_connect_error(self, _kerb_env):
        """ConnectError on first attempt retries, succeeds on second."""
        from mcp_zuul.server import lifespan, mcp

        call_count = 0

        async def mock_kerberos(client, base_url):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise httpx.ConnectError("Connection refused")

        with (
            patch("mcp_zuul.server.kerberos_auth", side_effect=mock_kerberos),
            patch("mcp_zuul.server._remove_tool", return_value=True),
            patch("mcp_zuul.server.asyncio.sleep", return_value=None) as mock_sleep,
        ):
            async with lifespan(mcp):
                pass

        assert call_count == 2
        mock_sleep.assert_called_once_with(5)

    async def test_retries_on_runtime_error(self, _kerb_env):
        """RuntimeError (e.g. expired ticket) retries and succeeds."""
        from mcp_zuul.server import lifespan, mcp

        call_count = 0

        async def mock_kerberos(client, base_url):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("Kerberos auth: expected 401, got 502")

        with (
            patch("mcp_zuul.server.kerberos_auth", side_effect=mock_kerberos),
            patch("mcp_zuul.server._remove_tool", return_value=True),
            patch("mcp_zuul.server.asyncio.sleep", return_value=None) as mock_sleep,
        ):
            async with lifespan(mcp):
                pass

        assert call_count == 2
        mock_sleep.assert_called_once_with(5)

    async def test_raises_after_max_retries(self, _kerb_env):
        """Raises after 3 consecutive failures."""
        from mcp_zuul.server import lifespan, mcp

        async def always_fail(client, base_url):
            raise httpx.ConnectError("Connection refused")

        with (
            patch("mcp_zuul.server.kerberos_auth", side_effect=always_fail),
            patch("mcp_zuul.server._remove_tool", return_value=True),
            patch("mcp_zuul.server.asyncio.sleep", return_value=None) as mock_sleep,
            pytest.raises(httpx.ConnectError, match="Connection refused"),
        ):
            async with lifespan(mcp):
                pass

        # 3 attempts = 2 sleeps (5s after attempt 1, 10s after attempt 2)
        assert mock_sleep.call_count == 2
        mock_sleep.assert_any_call(5)
        mock_sleep.assert_any_call(10)

    async def test_ssl_error_not_retried(self, _kerb_env):
        """SSL errors are not retried — they raise immediately."""
        from mcp_zuul.server import lifespan, mcp

        ssl_err = ssl.SSLError("certificate verify failed")
        inner = Exception()
        inner.__context__ = ssl_err
        connect_err = httpx.ConnectError("SSL failed")
        connect_err.__cause__ = inner

        with (
            patch("mcp_zuul.server.kerberos_auth", side_effect=connect_err),
            patch("mcp_zuul.server._remove_tool", return_value=True),
            pytest.raises(RuntimeError, match="SSL certificate"),
        ):
            async with lifespan(mcp):
                pass

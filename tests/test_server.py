"""Tests for server.py: BearerAuth, tool removal, tool listing, lifespan."""

import os
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
        server._tool_manager.remove_tool = MagicMock()
        assert _remove_tool(server, "get_build") is True
        server._tool_manager.remove_tool.assert_called_once_with("get_build")

    def test_returns_false_on_key_error(self):
        """Returns False when tool doesn't exist (KeyError)."""
        server = MagicMock()
        server._tool_manager.remove_tool.side_effect = KeyError("get_build")
        assert _remove_tool(server, "get_build") is False

    def test_returns_false_on_attribute_error(self):
        """Returns False when FastMCP API changes (AttributeError)."""
        server = MagicMock()
        server._tool_manager.remove_tool.side_effect = AttributeError
        assert _remove_tool(server, "get_build") is False


# ---------------------------------------------------------------------------
# _list_tool_names
# ---------------------------------------------------------------------------


class TestListToolNames:
    def test_lists_registered_tools(self):
        """Returns list of tool names from tool manager via list_tools()."""
        server = MagicMock()
        tool_a, tool_b, tool_c = MagicMock(), MagicMock(), MagicMock()
        tool_a.name = "get_build"
        tool_b.name = "list_builds"
        tool_c.name = "get_job"
        server._tool_manager.list_tools.return_value = [tool_a, tool_b, tool_c]
        result = _list_tool_names(server)
        assert sorted(result) == ["get_build", "get_job", "list_builds"]

    def test_returns_empty_on_attribute_error(self):
        """Returns empty list when FastMCP internal API changes."""
        server = MagicMock()
        server._tool_manager.list_tools.side_effect = AttributeError
        assert _list_tool_names(server) == []


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
        """ZUUL_READ_ONLY=true (default) removes enqueue/dequeue/autohold tools."""
        from mcp_zuul.server import lifespan, mcp

        removed = []

        def tracking_remove(server, name):
            removed.append(name)
            # Don't actually remove from real mcp (tools may not be registered)
            return True

        with patch("mcp_zuul.server._remove_tool", side_effect=tracking_remove):
            async with lifespan(mcp):
                pass

        assert set(removed) >= {"enqueue", "dequeue", "autohold_create", "autohold_delete"}

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

        write_tools = {"enqueue", "dequeue", "autohold_create", "autohold_delete"}
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

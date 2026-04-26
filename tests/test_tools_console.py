"""Tests for stream_build_console tool."""

import asyncio
import json
import ssl
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch


class _FakeClose:
    """Simulate a websockets Close frame."""

    def __init__(self, code: int, reason: str = ""):
        self.code = code
        self.reason = reason


def _make_ws(messages: list[str], *, block: bool = False) -> AsyncMock:
    """Create a mock WebSocket that yields messages.

    With block=False (default), the iterator ends after messages — fast tests.
    With block=True, blocks after messages until timeout fires — tests timeout behavior.
    """
    ws = AsyncMock()
    ws.send = AsyncMock()

    async def _aiter() -> AsyncIterator[str]:
        for m in messages:
            yield m
        if block:
            await asyncio.sleep(999)

    ws.__aenter__ = AsyncMock(return_value=ws)
    ws.__aexit__ = AsyncMock(return_value=False)
    ws.__aiter__ = lambda self: _aiter()
    return ws


def _mock_ws_module(connect_rv=None, connect_side_effect=None):
    """Build a mock websockets module with exception classes."""
    m = MagicMock()
    m.InvalidStatus = type("InvalidStatus", (Exception,), {})
    m.ConnectionClosedError = type("ConnectionClosedError", (Exception,), {})
    if connect_side_effect:
        m.connect = MagicMock(side_effect=connect_side_effect)
    elif connect_rv is not None:
        m.connect = MagicMock(return_value=connect_rv)
    return m


class TestStreamBuildConsole:
    """Tests for stream_build_console tool."""

    async def test_missing_dependency(self, mock_ctx):
        """When websockets is not installed, return clear install instructions."""
        import sys

        original_import = (
            __builtins__.__import__ if hasattr(__builtins__, "__import__") else __import__
        )

        def _fail_websockets(name, *args, **kwargs):
            if name == "websockets":
                raise ImportError("No module named 'websockets'")
            return original_import(name, *args, **kwargs)

        # Clear sys.modules cache so the lazy import actually calls __import__
        saved = sys.modules.pop("websockets", None)
        try:
            with patch("builtins.__import__", side_effect=_fail_websockets):
                from mcp_zuul.tools._console import stream_build_console

                result = json.loads(
                    await stream_build_console(mock_ctx, uuid="abc123", tenant="test-tenant")
                )
                assert "error" in result
                assert "websockets" in result["error"]
                assert "mcp-zuul[console]" in result["error"]
        finally:
            if saved is not None:
                sys.modules["websockets"] = saved

    async def test_basic_streaming(self, mock_ctx):
        """Basic: mock WS sends 3 lines, all returned."""
        from mcp_zuul.tools._console import stream_build_console

        ws = _make_ws(
            [
                "TASK [setup : install] ***\n",
                "ok: [controller-0]\n",
                "TASK [validate : check] ***\n",
            ]
        )
        mod = _mock_ws_module(connect_rv=ws)

        with patch("mcp_zuul.tools._console._import_websockets", return_value=mod):
            result = json.loads(
                await stream_build_console(mock_ctx, uuid="abc123", tenant="test-tenant", timeout=3)
            )

        assert "console" in result
        assert result["lines_returned"] == 3
        assert result["total_lines_received"] == 3
        assert result["build_uuid"] == "abc123"
        assert result["tenant"] == "test-tenant"
        assert "TASK [setup : install]" in result["console"]
        assert "TASK [validate : check]" in result["console"]

    async def test_tail_behavior(self, mock_ctx):
        """When more lines received than requested, only tail is returned."""
        from mcp_zuul.tools._console import stream_build_console

        messages = [f"line {i}\n" for i in range(200)]
        ws = _make_ws(messages)
        mod = _mock_ws_module(connect_rv=ws)

        with patch("mcp_zuul.tools._console._import_websockets", return_value=mod):
            result = json.loads(
                await stream_build_console(
                    mock_ctx, uuid="abc123", tenant="test-tenant", lines=50, timeout=3
                )
            )

        assert result["lines_returned"] == 50
        assert result["total_lines_received"] == 200
        assert result["tail"] is True
        # Should contain the LAST 50 lines (150-199)
        assert "line 199" in result["console"]
        assert "line 150" in result["console"]
        assert "line 149" not in result["console"]

    async def test_empty_stream(self, mock_ctx):
        """When no messages received, return error."""
        from mcp_zuul.tools._console import stream_build_console

        ws = _make_ws([])
        mod = _mock_ws_module(connect_rv=ws)

        with patch("mcp_zuul.tools._console._import_websockets", return_value=mod):
            result = json.loads(
                await stream_build_console(mock_ctx, uuid="abc123", tenant="test-tenant", timeout=3)
            )

        assert "error" in result
        assert "no console output" in result["error"].lower()

    async def test_auth_token_forwarded(self, mock_ctx):
        """When config has auth_token, it's included in the first WS message."""
        from mcp_zuul.tools._console import stream_build_console

        mock_ctx.request_context.lifespan_context.config.auth_token = "my-jwt-token"
        ws = _make_ws(["some output\n"])
        mod = _mock_ws_module(connect_rv=ws)

        with patch("mcp_zuul.tools._console._import_websockets", return_value=mod):
            await stream_build_console(mock_ctx, uuid="abc123", tenant="test-tenant", timeout=3)

        ws.send.assert_called_once()
        sent = json.loads(ws.send.call_args[0][0])
        assert sent["token"] == "my-jwt-token"
        assert sent["uuid"] == "abc123"
        assert sent["logfile"] == "console.log"

        mock_ctx.request_context.lifespan_context.config.auth_token = None

    async def test_no_auth_token_omitted(self, mock_ctx):
        """When no auth_token, the token field is not in the first message."""
        from mcp_zuul.tools._console import stream_build_console

        ws = _make_ws(["output\n"])
        mod = _mock_ws_module(connect_rv=ws)

        with patch("mcp_zuul.tools._console._import_websockets", return_value=mod):
            await stream_build_console(mock_ctx, uuid="abc123", tenant="test-tenant", timeout=3)

        sent = json.loads(ws.send.call_args[0][0])
        assert "token" not in sent

    async def test_http_403_on_upgrade(self, mock_ctx):
        """InvalidStatus with 403 -> auth failed error."""
        from mcp_zuul.tools._console import stream_build_console

        mod = _mock_ws_module()
        exc = mod.InvalidStatus()
        exc.response = MagicMock()
        exc.response.status_code = 403
        mod.connect = MagicMock(side_effect=exc)

        with patch("mcp_zuul.tools._console._import_websockets", return_value=mod):
            result = json.loads(
                await stream_build_console(mock_ctx, uuid="abc123", tenant="test-tenant", timeout=3)
            )

        assert "error" in result
        assert "403" in result["error"] or "auth" in result["error"].lower()

    async def test_http_404_on_upgrade(self, mock_ctx):
        """InvalidStatus with 404 -> build completed or not available."""
        from mcp_zuul.tools._console import stream_build_console

        mod = _mock_ws_module()
        exc = mod.InvalidStatus()
        exc.response = MagicMock()
        exc.response.status_code = 404
        mod.connect = MagicMock(side_effect=exc)

        with patch("mcp_zuul.tools._console._import_websockets", return_value=mod):
            result = json.loads(
                await stream_build_console(mock_ctx, uuid="abc123", tenant="test-tenant", timeout=3)
            )

        assert "error" in result
        assert "completed" in result["error"].lower() or "not available" in result["error"].lower()

    async def test_connection_closed_4000(self, mock_ctx):
        """ConnectionClosedError with code 4000 -> validation/auth error with reason."""
        from mcp_zuul.tools._console import stream_build_console

        mod = _mock_ws_module()
        exc = mod.ConnectionClosedError()
        exc.rcvd = _FakeClose(4000, "'uuid' missing from request payload")

        ws = AsyncMock()
        ws.__aenter__ = AsyncMock(return_value=ws)
        ws.__aexit__ = AsyncMock(return_value=False)
        ws.send = AsyncMock()

        async def _raise_on_iter() -> AsyncIterator[str]:
            raise exc
            yield  # make it a generator

        ws.__aiter__ = lambda self: _raise_on_iter()
        mod.connect = MagicMock(return_value=ws)

        with patch("mcp_zuul.tools._console._import_websockets", return_value=mod):
            result = json.loads(
                await stream_build_console(mock_ctx, uuid="abc123", tenant="test-tenant", timeout=3)
            )

        assert "error" in result
        assert "4000" in result["error"]
        assert "missing" in result["error"].lower()

    async def test_connection_closed_4011(self, mock_ctx):
        """ConnectionClosedError with code 4011 -> streaming error."""
        from mcp_zuul.tools._console import stream_build_console

        mod = _mock_ws_module()
        exc = mod.ConnectionClosedError()
        exc.rcvd = _FakeClose(4011, "streaming error")

        ws = AsyncMock()
        ws.__aenter__ = AsyncMock(return_value=ws)
        ws.__aexit__ = AsyncMock(return_value=False)
        ws.send = AsyncMock()

        async def _raise_on_iter() -> AsyncIterator[str]:
            raise exc
            yield  # make it a generator

        ws.__aiter__ = lambda self: _raise_on_iter()
        mod.connect = MagicMock(return_value=ws)

        with patch("mcp_zuul.tools._console._import_websockets", return_value=mod):
            result = json.loads(
                await stream_build_console(mock_ctx, uuid="abc123", tenant="test-tenant", timeout=3)
            )

        assert "error" in result
        assert "4011" in result["error"] or "streaming" in result["error"].lower()

    async def test_connection_closed_mid_stream(self, mock_ctx):
        """ConnectionClosedError after some data: buffered lines are lost, error returned."""
        from mcp_zuul.tools._console import stream_build_console

        mod = _mock_ws_module()
        exc = mod.ConnectionClosedError()
        exc.rcvd = _FakeClose(4011, "executor died")

        ws = AsyncMock()
        ws.__aenter__ = AsyncMock(return_value=ws)
        ws.__aexit__ = AsyncMock(return_value=False)
        ws.send = AsyncMock()

        async def _yield_then_die() -> AsyncIterator[str]:
            yield "line 1\n"
            yield "line 2\n"
            raise exc

        ws.__aiter__ = lambda self: _yield_then_die()
        mod.connect = MagicMock(return_value=ws)

        with patch("mcp_zuul.tools._console._import_websockets", return_value=mod):
            result = json.loads(
                await stream_build_console(mock_ctx, uuid="abc123", tenant="test-tenant", timeout=3)
            )

        # ConnectionClosedError propagates past the buffer -> error, not partial data
        assert "error" in result
        assert "4011" in result["error"]

    async def test_connection_refused(self, mock_ctx):
        """ConnectionRefusedError -> clear error message."""
        from mcp_zuul.tools._console import stream_build_console

        mod = _mock_ws_module(connect_side_effect=ConnectionRefusedError("Connection refused"))

        with patch("mcp_zuul.tools._console._import_websockets", return_value=mod):
            result = json.loads(
                await stream_build_console(mock_ctx, uuid="abc123", tenant="test-tenant", timeout=3)
            )

        assert "error" in result
        assert "connect" in result["error"].lower() or "refused" in result["error"].lower()

    async def test_ansi_stripping(self, mock_ctx):
        """ANSI escape codes are stripped from console output."""
        from mcp_zuul.tools._console import stream_build_console

        ws = _make_ws(["\x1b[32mok\x1b[0m: [controller-0]\n"])
        mod = _mock_ws_module(connect_rv=ws)

        with patch("mcp_zuul.tools._console._import_websockets", return_value=mod):
            result = json.loads(
                await stream_build_console(mock_ctx, uuid="abc123", tenant="test-tenant", timeout=3)
            )

        assert "\x1b" not in result["console"]
        assert "ok: [controller-0]" in result["console"]

    async def test_bytes_message_decoded(self, mock_ctx):
        """Binary WebSocket frames are decoded as UTF-8, not repr'd."""
        from mcp_zuul.tools._console import stream_build_console

        ws = AsyncMock()
        ws.__aenter__ = AsyncMock(return_value=ws)
        ws.__aexit__ = AsyncMock(return_value=False)
        ws.send = AsyncMock()

        async def _yield_bytes() -> AsyncIterator[bytes]:
            yield b"binary line\n"

        ws.__aiter__ = lambda self: _yield_bytes()
        mod = _mock_ws_module(connect_rv=ws)

        with patch("mcp_zuul.tools._console._import_websockets", return_value=mod):
            result = json.loads(
                await stream_build_console(mock_ctx, uuid="abc123", tenant="test-tenant", timeout=3)
            )

        assert result["lines_returned"] == 1
        assert "binary line" in result["console"]
        assert "b'" not in result["console"]  # Not repr'd

    async def test_lines_clamped_low(self, mock_ctx):
        """lines=0 is clamped to 1."""
        from mcp_zuul.tools._console import stream_build_console

        ws = _make_ws(["line1\n", "line2\n", "line3\n"])
        mod = _mock_ws_module(connect_rv=ws)

        with patch("mcp_zuul.tools._console._import_websockets", return_value=mod):
            result = json.loads(
                await stream_build_console(
                    mock_ctx, uuid="abc123", tenant="test-tenant", lines=0, timeout=3
                )
            )

        assert result["lines_returned"] == 1
        assert result["tail"] is True

    async def test_lines_clamped_high(self, mock_ctx):
        """lines=999 is clamped to 500."""
        from mcp_zuul.tools._console import stream_build_console

        ws = _make_ws(["line\n"])
        mod = _mock_ws_module(connect_rv=ws)

        with patch("mcp_zuul.tools._console._import_websockets", return_value=mod):
            result = json.loads(
                await stream_build_console(
                    mock_ctx, uuid="abc123", tenant="test-tenant", lines=999, timeout=3
                )
            )

        assert result["lines_returned"] == 1
        assert result["tail"] is False

    async def test_ws_url_construction(self, mock_ctx):
        """WebSocket URL is built correctly from config.base_url."""
        from mcp_zuul.tools._console import stream_build_console

        ws = _make_ws(["output\n"])
        mod = _mock_ws_module(connect_rv=ws)

        with patch("mcp_zuul.tools._console._import_websockets", return_value=mod):
            await stream_build_console(mock_ctx, uuid="abc123", tenant="test-tenant", timeout=3)

        ws_url = mod.connect.call_args[0][0]
        assert ws_url == "wss://zuul.example.com/api/tenant/test-tenant/console-stream"

    async def test_ws_url_http_to_ws(self, mock_ctx):
        """http:// base URL maps to ws:// WebSocket URL."""
        from mcp_zuul.tools._console import stream_build_console

        mock_ctx.request_context.lifespan_context.config.base_url = "http://zuul.local"
        ws = _make_ws(["output\n"])
        mod = _mock_ws_module(connect_rv=ws)

        with patch("mcp_zuul.tools._console._import_websockets", return_value=mod):
            await stream_build_console(mock_ctx, uuid="abc123", tenant="test-tenant", timeout=3)

        ws_url = mod.connect.call_args[0][0]
        assert ws_url.startswith("ws://")
        assert ws_url == "ws://zuul.local/api/tenant/test-tenant/console-stream"
        # ws:// should NOT have SSL context
        assert mod.connect.call_args[1].get("ssl") is None

        mock_ctx.request_context.lifespan_context.config.base_url = "https://zuul.example.com"

    async def test_ssl_context_verify_false(self, mock_ctx):
        """When verify_ssl=False, SSL context disables cert verification."""
        from mcp_zuul.tools._console import stream_build_console

        mock_ctx.request_context.lifespan_context.config.verify_ssl = False
        ws = _make_ws(["output\n"])
        mod = _mock_ws_module(connect_rv=ws)

        with patch("mcp_zuul.tools._console._import_websockets", return_value=mod):
            await stream_build_console(mock_ctx, uuid="abc123", tenant="test-tenant", timeout=3)

        call_kwargs = mod.connect.call_args[1]
        ssl_ctx = call_kwargs.get("ssl")
        assert ssl_ctx is not None
        assert ssl_ctx.check_hostname is False
        assert ssl_ctx.verify_mode == ssl.CERT_NONE

        mock_ctx.request_context.lifespan_context.config.verify_ssl = True

    async def test_ssl_context_verify_true(self, mock_ctx):
        """When verify_ssl=True (default), ssl=True is passed for default context."""
        from mcp_zuul.tools._console import stream_build_console

        ws = _make_ws(["output\n"])
        mod = _mock_ws_module(connect_rv=ws)

        with patch("mcp_zuul.tools._console._import_websockets", return_value=mod):
            await stream_build_console(mock_ctx, uuid="abc123", tenant="test-tenant", timeout=3)

        call_kwargs = mod.connect.call_args[1]
        assert call_kwargs.get("ssl") is True

    async def test_uuid_required(self, mock_ctx):
        """When no uuid and no url, return error."""
        from mcp_zuul.tools._console import stream_build_console

        result = json.loads(await stream_build_console(mock_ctx, tenant="test-tenant", timeout=3))
        assert "error" in result
        assert "required" in result["error"].lower()

    async def test_url_param_parses_build(self, mock_ctx):
        """url parameter extracts UUID and tenant from Zuul build URL."""
        from mcp_zuul.tools._console import stream_build_console

        ws = _make_ws(["output\n"])
        mod = _mock_ws_module(connect_rv=ws)

        with patch("mcp_zuul.tools._console._import_websockets", return_value=mod):
            result = json.loads(
                await stream_build_console(
                    mock_ctx,
                    url="https://zuul.example.com/t/my-tenant/build/deadbeef-1234",
                    timeout=3,
                )
            )

        assert result["build_uuid"] == "deadbeef-1234"
        assert result["tenant"] == "my-tenant"
        # Verify WS URL uses parsed tenant
        ws_url = mod.connect.call_args[0][0]
        assert "/tenant/my-tenant/" in ws_url

    async def test_multiline_messages(self, mock_ctx):
        """A single WS message containing multiple lines is split correctly."""
        from mcp_zuul.tools._console import stream_build_console

        ws = _make_ws(["line1\nline2\nline3\n"])
        mod = _mock_ws_module(connect_rv=ws)

        with patch("mcp_zuul.tools._console._import_websockets", return_value=mod):
            result = json.loads(
                await stream_build_console(mock_ctx, uuid="abc123", tenant="test-tenant", timeout=3)
            )

        assert result["total_lines_received"] == 3
        assert result["lines_returned"] == 3
        assert "line1" in result["console"]
        assert "line3" in result["console"]

    async def test_normal_close_returns_buffered(self, mock_ctx):
        """When server sends close(1000), buffered lines are returned."""
        from mcp_zuul.tools._console import stream_build_console

        ws = AsyncMock()
        ws.__aenter__ = AsyncMock(return_value=ws)
        ws.__aexit__ = AsyncMock(return_value=False)
        ws.send = AsyncMock()

        async def _iter_then_close() -> AsyncIterator[str]:
            yield "task running\n"
            yield "task complete\n"

        ws.__aiter__ = lambda self: _iter_then_close()
        mod = _mock_ws_module(connect_rv=ws)

        with patch("mcp_zuul.tools._console._import_websockets", return_value=mod):
            result = json.loads(
                await stream_build_console(mock_ctx, uuid="abc123", tenant="test-tenant", timeout=3)
            )

        assert result["lines_returned"] == 2
        assert "task complete" in result["console"]

    async def test_tenant_path_traversal_rejected(self, mock_ctx):
        """Tenant with '..' is rejected by safepath before WebSocket connect."""
        from mcp_zuul.tools._console import stream_build_console

        mod = _mock_ws_module()

        with patch("mcp_zuul.tools._console._import_websockets", return_value=mod):
            result = json.loads(
                await stream_build_console(mock_ctx, uuid="abc123", tenant="../admin", timeout=3)
            )
        assert "error" in result
        assert "invalid" in result["error"].lower() or "path" in result["error"].lower()

    async def test_os_error_from_connect(self, mock_ctx):
        """Generic OSError (e.g. network unreachable) during connect is handled."""
        from mcp_zuul.tools._console import stream_build_console

        mod = _mock_ws_module(connect_side_effect=OSError("Network is unreachable"))

        with patch("mcp_zuul.tools._console._import_websockets", return_value=mod):
            result = json.loads(
                await stream_build_console(mock_ctx, uuid="abc123", tenant="test-tenant", timeout=3)
            )

        assert "error" in result
        assert "connect" in result["error"].lower() or "unreachable" in result["error"].lower()

    async def test_timeout_stops_reading(self, mock_ctx):
        """The timeout fires to stop reading from a live (blocking) stream."""
        from mcp_zuul.tools._console import stream_build_console

        ws = _make_ws(["live output\n"], block=True)
        mod = _mock_ws_module(connect_rv=ws)

        with patch("mcp_zuul.tools._console._import_websockets", return_value=mod):
            result = json.loads(
                await stream_build_console(mock_ctx, uuid="abc123", tenant="test-tenant", timeout=3)
            )

        assert result["lines_returned"] == 1
        assert result["timeout_seconds"] == 3
        assert "live output" in result["console"]

    async def test_invalid_base_url_scheme(self, mock_ctx):
        """Non-http/https base URL returns clear error."""
        from mcp_zuul.tools._console import stream_build_console

        mock_ctx.request_context.lifespan_context.config.base_url = "ftp://zuul.example.com"
        mod = _mock_ws_module()

        with patch("mcp_zuul.tools._console._import_websockets", return_value=mod):
            result = json.loads(
                await stream_build_console(mock_ctx, uuid="abc123", tenant="test-tenant", timeout=3)
            )

        assert "error" in result
        assert "websocket url" in result["error"].lower() or "ftp" in result["error"].lower()

        mock_ctx.request_context.lifespan_context.config.base_url = "https://zuul.example.com"

    async def test_generic_http_error_on_upgrade(self, mock_ctx):
        """InvalidStatus with unexpected code (e.g. 500) returns generic error."""
        from mcp_zuul.tools._console import stream_build_console

        mod = _mock_ws_module()
        exc = mod.InvalidStatus()
        exc.response = MagicMock()
        exc.response.status_code = 500
        mod.connect = MagicMock(side_effect=exc)

        with patch("mcp_zuul.tools._console._import_websockets", return_value=mod):
            result = json.loads(
                await stream_build_console(mock_ctx, uuid="abc123", tenant="test-tenant", timeout=3)
            )

        assert "error" in result
        assert "500" in result["error"]

    async def test_generic_connection_close_code(self, mock_ctx):
        """ConnectionClosedError with unexpected code returns generic message."""
        from mcp_zuul.tools._console import stream_build_console

        mod = _mock_ws_module()
        exc = mod.ConnectionClosedError()
        exc.rcvd = _FakeClose(1006, "abnormal closure")

        ws = AsyncMock()
        ws.__aenter__ = AsyncMock(return_value=ws)
        ws.__aexit__ = AsyncMock(return_value=False)
        ws.send = AsyncMock()

        async def _raise_on_iter() -> AsyncIterator[str]:
            raise exc
            yield  # make it a generator

        ws.__aiter__ = lambda self: _raise_on_iter()
        mod.connect = MagicMock(return_value=ws)

        with patch("mcp_zuul.tools._console._import_websockets", return_value=mod):
            result = json.loads(
                await stream_build_console(mock_ctx, uuid="abc123", tenant="test-tenant", timeout=3)
            )

        assert "error" in result
        assert "1006" in result["error"]
        assert "abnormal" in result["error"].lower()

    async def test_partial_lines_reassembled(self, mock_ctx):
        """Chunks splitting mid-line are reassembled correctly."""
        from mcp_zuul.tools._console import stream_build_console

        # Simulate Zuul sending 4096-byte chunks that split mid-line
        ws = AsyncMock()
        ws.__aenter__ = AsyncMock(return_value=ws)
        ws.__aexit__ = AsyncMock(return_value=False)
        ws.send = AsyncMock()

        async def _chunked() -> AsyncIterator[str]:
            yield "TASK [setup : inst"  # chunk ends mid-word
            yield "all_packages] ***\nok: [controller-0]\n"  # completes previous + new line
            yield "TASK [val"
            yield "idate] ***\n"

        ws.__aiter__ = lambda self: _chunked()
        mod = _mock_ws_module(connect_rv=ws)

        with patch("mcp_zuul.tools._console._import_websockets", return_value=mod):
            result = json.loads(
                await stream_build_console(mock_ctx, uuid="abc123", tenant="test-tenant", timeout=3)
            )

        assert result["total_lines_received"] == 3
        assert result["lines_returned"] == 3
        # Lines should be properly reassembled, not split at chunk boundaries
        lines = result["console"].split("\n")
        assert "TASK [setup : install_packages] ***" in lines
        assert "ok: [controller-0]" in lines
        assert "TASK [validate] ***" in lines

    async def test_trailing_partial_line_flushed(self, mock_ctx):
        """A partial line at the end of stream (no trailing newline) is included."""
        from mcp_zuul.tools._console import stream_build_console

        ws = AsyncMock()
        ws.__aenter__ = AsyncMock(return_value=ws)
        ws.__aexit__ = AsyncMock(return_value=False)
        ws.send = AsyncMock()

        async def _no_trailing_newline() -> AsyncIterator[str]:
            yield "line1\nline2_no_newline"

        ws.__aiter__ = lambda self: _no_trailing_newline()
        mod = _mock_ws_module(connect_rv=ws)

        with patch("mcp_zuul.tools._console._import_websockets", return_value=mod):
            result = json.loads(
                await stream_build_console(mock_ctx, uuid="abc123", tenant="test-tenant", timeout=3)
            )

        assert result["total_lines_received"] == 2
        assert "line2_no_newline" in result["console"]

    async def test_open_timeout_produces_clear_error(self, mock_ctx):
        """open_timeout failure returns timeout-specific error, not generic OSError."""
        from mcp_zuul.tools._console import stream_build_console

        mod = _mock_ws_module(connect_side_effect=TimeoutError("timed out"))

        with patch("mcp_zuul.tools._console._import_websockets", return_value=mod):
            result = json.loads(
                await stream_build_console(mock_ctx, uuid="abc123", tenant="test-tenant", timeout=3)
            )

        assert "error" in result
        assert "timed out" in result["error"].lower()

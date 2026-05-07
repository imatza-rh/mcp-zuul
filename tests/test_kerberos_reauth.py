"""Tests for Kerberos re-authentication in api(), _api_mutate(), and _stream_response().

These tests verify the critical security mechanism that re-authenticates
when a session expires (401 response) while preventing thundering herd
via an asyncio.Lock + generation counter.
"""

import asyncio
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from mcp_zuul.helpers import api, api_delete, api_post, fetch_log_url, stream_log

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def krb_ctx(mock_ctx):
    """Enable Kerberos on the existing mock_ctx."""
    mock_ctx.request_context.lifespan_context.config.use_kerberos = True
    mock_ctx.request_context.lifespan_context.config.read_only = False
    return mock_ctx


# ---------------------------------------------------------------------------
# api() GET re-auth
# ---------------------------------------------------------------------------


class TestApiKerberosReauth:
    """Verify api() re-authenticates on 401 when Kerberos is enabled."""

    @respx.mock
    async def test_reauths_on_401_then_succeeds(self, krb_ctx):
        """401 → re-auth → retry GET → 200."""
        route = respx.get("https://zuul.example.com/api/tenants")
        route.side_effect = [
            httpx.Response(401, text="Unauthorized"),
            httpx.Response(200, json=[{"name": "t1"}]),
        ]
        with patch("mcp_zuul.helpers.kerberos_auth", new_callable=AsyncMock) as mock_auth:
            result = await api(krb_ctx, "/tenants")

        assert result == [{"name": "t1"}]
        mock_auth.assert_awaited_once()
        assert krb_ctx.request_context.lifespan_context._auth_generation == 1

    @respx.mock
    async def test_no_reauth_without_kerberos(self, mock_ctx):
        """401 without Kerberos enabled raises directly (no re-auth)."""
        assert mock_ctx.request_context.lifespan_context.config.use_kerberos is False
        respx.get("https://zuul.example.com/api/tenants").mock(
            return_value=httpx.Response(401, text="Unauthorized")
        )
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await api(mock_ctx, "/tenants")
        assert exc_info.value.response.status_code == 401

    @respx.mock
    async def test_raises_if_retry_also_401(self, krb_ctx):
        """If the retry after re-auth also returns 401, raise (no infinite loop)."""
        respx.get("https://zuul.example.com/api/tenants").mock(
            return_value=httpx.Response(401, text="Unauthorized")
        )
        with (
            patch("mcp_zuul.helpers.kerberos_auth", new_callable=AsyncMock),
            pytest.raises(httpx.HTTPStatusError) as exc_info,
        ):
            await api(krb_ctx, "/tenants")
        assert exc_info.value.response.status_code == 401

    @respx.mock
    async def test_generation_counter_increments(self, krb_ctx):
        """Re-auth should increment _auth_generation."""
        a = krb_ctx.request_context.lifespan_context
        assert a._auth_generation == 0

        route = respx.get("https://zuul.example.com/api/tenants")
        route.side_effect = [
            httpx.Response(401, text="Unauthorized"),
            httpx.Response(200, json={"ok": True}),
        ]
        with patch("mcp_zuul.helpers.kerberos_auth", new_callable=AsyncMock):
            await api(krb_ctx, "/tenants")

        assert a._auth_generation == 1

    @respx.mock
    async def test_generation_skip_when_already_bumped(self, krb_ctx):
        """If generation was bumped while waiting for lock, skip re-auth.

        Simulates: request captures gen=0, but by the time it acquires the
        lock another coroutine already re-authed and bumped gen to 1.
        The ``if a._auth_generation == gen`` check prevents redundant re-auth.
        """
        a = krb_ctx.request_context.lifespan_context
        assert a._auth_generation == 0

        route = respx.get("https://zuul.example.com/api/tenants")
        route.side_effect = [
            httpx.Response(401, text="Unauthorized"),
            # After skipping re-auth, the retry still happens
            httpx.Response(200, json={"ok": True}),
        ]

        original_lock_acquire = a._auth_lock.acquire

        async def bump_gen_before_lock():
            """Simulate another coroutine bumping generation before we get the lock."""
            # Bump generation as if another request already re-authed
            a._auth_generation = 1
            return await original_lock_acquire()

        with (
            patch.object(a._auth_lock, "acquire", side_effect=bump_gen_before_lock),
            patch("mcp_zuul.helpers.kerberos_auth", new_callable=AsyncMock) as mock_auth,
        ):
            await api(krb_ctx, "/tenants")

        # Re-auth was NOT called — generation mismatch caused skip
        mock_auth.assert_not_awaited()
        # Generation remains at 1 (bumped by simulated other coroutine)
        assert a._auth_generation == 1

    @respx.mock
    async def test_concurrent_401s_single_reauth(self, krb_ctx):
        """Two concurrent requests hitting 401 should only trigger one re-auth.

        This is the thundering herd prevention test:
        - Both requests get 401 and capture gen=0
        - First request acquires lock, re-auths, bumps gen to 1
        - Second request acquires lock, sees gen=0 != current 1, skips re-auth
        """
        a = krb_ctx.request_context.lifespan_context
        assert a._auth_generation == 0

        reauth_count = 0

        async def slow_kerberos_auth(client, base_url):
            nonlocal reauth_count
            reauth_count += 1
            # Simulate auth taking some time
            await asyncio.sleep(0.05)

        call_index = 0

        def response_factory(request):
            nonlocal call_index
            call_index += 1
            # First two calls (one per coroutine) return 401
            # All subsequent calls return 200
            if call_index <= 2:
                return httpx.Response(401, text="Unauthorized")
            return httpx.Response(200, json={"ok": True})

        respx.get("https://zuul.example.com/api/tenants").mock(side_effect=response_factory)

        with patch("mcp_zuul.helpers.kerberos_auth", side_effect=slow_kerberos_auth):
            results = await asyncio.gather(
                api(krb_ctx, "/tenants"),
                api(krb_ctx, "/tenants"),
            )

        assert all(r == {"ok": True} for r in results)
        # Only ONE re-auth despite TWO 401s
        assert reauth_count == 1
        assert a._auth_generation == 1


# ---------------------------------------------------------------------------
# _api_mutate() POST/DELETE re-auth
# ---------------------------------------------------------------------------


class TestApiMutateKerberosReauth:
    """Verify _api_mutate() re-authenticates on 401 for POST and DELETE."""

    @respx.mock
    async def test_post_reauths_on_401(self, krb_ctx):
        """POST 401 → re-auth → retry POST → 200."""
        route = respx.post("https://zuul.example.com/api/tenant/test-tenant/enqueue")
        route.side_effect = [
            httpx.Response(401, text="Unauthorized"),
            httpx.Response(200, json={"status": "ok"}),
        ]
        with patch("mcp_zuul.helpers.kerberos_auth", new_callable=AsyncMock) as mock_auth:
            result = await api_post(krb_ctx, "/tenant/test-tenant/enqueue", {"change": "1"})

        assert result == {"status": "ok"}
        mock_auth.assert_awaited_once()
        assert krb_ctx.request_context.lifespan_context._auth_generation == 1

    @respx.mock
    async def test_delete_reauths_on_401(self, krb_ctx):
        """DELETE 401 → re-auth → retry DELETE → 204."""
        route = respx.delete("https://zuul.example.com/api/tenant/test-tenant/autohold/42")
        route.side_effect = [
            httpx.Response(401, text="Unauthorized"),
            httpx.Response(204),
        ]
        with patch("mcp_zuul.helpers.kerberos_auth", new_callable=AsyncMock) as mock_auth:
            result = await api_delete(krb_ctx, "/tenant/test-tenant/autohold/42")

        assert result == {}
        mock_auth.assert_awaited_once()

    @respx.mock
    async def test_mutate_no_reauth_without_kerberos(self, mock_ctx):
        """POST 401 without Kerberos raises directly."""
        mock_ctx.request_context.lifespan_context.config.read_only = False
        respx.post("https://zuul.example.com/api/tenant/test-tenant/enqueue").mock(
            return_value=httpx.Response(401, text="Unauthorized")
        )
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await api_post(mock_ctx, "/tenant/test-tenant/enqueue", {"change": "1"})
        assert exc_info.value.response.status_code == 401

    @respx.mock
    async def test_mutate_raises_if_retry_also_401(self, krb_ctx):
        """If POST retry after re-auth also 401, raise."""
        respx.post("https://zuul.example.com/api/tenant/test-tenant/enqueue").mock(
            return_value=httpx.Response(401, text="Unauthorized")
        )
        with (
            patch("mcp_zuul.helpers.kerberos_auth", new_callable=AsyncMock),
            pytest.raises(httpx.HTTPStatusError) as exc_info,
        ):
            await api_post(krb_ctx, "/tenant/test-tenant/enqueue", {"change": "1"})
        assert exc_info.value.response.status_code == 401

    @respx.mock
    async def test_mutate_reauth_then_503_retries(self, krb_ctx):
        """POST 401 → re-auth → retry → 503 → retry again → 200.

        Verifies that 500/503 retry still works after a re-auth cycle.
        """
        route = respx.post("https://zuul.example.com/api/tenant/test-tenant/enqueue")
        route.side_effect = [
            httpx.Response(401, text="Unauthorized"),
            # After re-auth, retry returns 503 on first loop iteration
            httpx.Response(503, text="Service Unavailable"),
            # Second loop iteration succeeds
            httpx.Response(200, json={"status": "ok"}),
        ]
        with patch("mcp_zuul.helpers.kerberos_auth", new_callable=AsyncMock):
            result = await api_post(krb_ctx, "/tenant/test-tenant/enqueue", {"change": "1"})
        assert result == {"status": "ok"}

    @respx.mock
    async def test_mutate_generation_prevents_duplicate_reauth(self, krb_ctx):
        """Two concurrent POST requests hitting 401 → only one re-auth."""
        a = krb_ctx.request_context.lifespan_context
        a.config.read_only = False

        reauth_count = 0

        async def slow_kerberos_auth(client, base_url):
            nonlocal reauth_count
            reauth_count += 1
            await asyncio.sleep(0.05)

        call_index = 0

        def response_factory(request):
            nonlocal call_index
            call_index += 1
            if call_index <= 2:
                return httpx.Response(401, text="Unauthorized")
            return httpx.Response(200, json={"status": "ok"})

        respx.post("https://zuul.example.com/api/tenant/test-tenant/enqueue").mock(
            side_effect=response_factory
        )

        with patch("mcp_zuul.helpers.kerberos_auth", side_effect=slow_kerberos_auth):
            results = await asyncio.gather(
                api_post(krb_ctx, "/tenant/test-tenant/enqueue", {"a": 1}),
                api_post(krb_ctx, "/tenant/test-tenant/enqueue", {"b": 2}),
            )

        assert all(r == {"status": "ok"} for r in results)
        assert reauth_count == 1
        assert a._auth_generation == 1

    @respx.mock
    async def test_post_redirect_triggers_reauth(self, krb_ctx):
        """POST 302 (OIDC redirect) → re-auth → retry POST → 200.

        Without follow_redirects=False, httpx would convert POST to GET on 302,
        losing the request body. The fix detects 302 and triggers re-auth instead.
        """
        route = respx.post("https://zuul.example.com/api/tenant/test-tenant/enqueue")
        route.side_effect = [
            httpx.Response(302, headers={"location": "https://sso.example.com/auth"}),
            httpx.Response(200, json={"status": "ok"}),
        ]
        with patch("mcp_zuul.helpers.kerberos_auth", new_callable=AsyncMock) as mock_auth:
            result = await api_post(krb_ctx, "/tenant/test-tenant/enqueue", {"change": "1"})

        assert result == {"status": "ok"}
        mock_auth.assert_awaited_once()

    @respx.mock
    async def test_post_redirect_raises_if_retry_also_redirects(self, krb_ctx):
        """POST 302 → re-auth → retry → 302 again → raises (no infinite loop)."""
        respx.post("https://zuul.example.com/api/tenant/test-tenant/enqueue").mock(
            return_value=httpx.Response(302, headers={"location": "https://sso.example.com/auth"})
        )
        with (
            patch("mcp_zuul.helpers.kerberos_auth", new_callable=AsyncMock),
            pytest.raises(httpx.HTTPStatusError) as exc_info,
        ):
            await api_post(krb_ctx, "/tenant/test-tenant/enqueue", {"change": "1"})
        assert exc_info.value.response.status_code == 302


# ---------------------------------------------------------------------------
# _stream_response() re-auth (via fetch_log_url / stream_log)
# ---------------------------------------------------------------------------


class TestStreamResponseKerberosReauth:
    """Verify _stream_response() re-authenticates on 401 for log streaming.

    The streaming re-auth only triggers when the log is served from the
    API host (same origin) — cross-origin log hosts use log_client which
    has no auth, so 401 is not retried.
    """

    @respx.mock
    async def test_fetch_log_reauths_same_origin(self, krb_ctx):
        """401 from API host → re-auth → retry → 200."""
        a = krb_ctx.request_context.lifespan_context
        # Log URL on the same host as the API
        url = "https://zuul.example.com/logs/build/job-output.json"

        route = respx.get(url)
        route.side_effect = [
            httpx.Response(401, text="Unauthorized"),
            httpx.Response(200, content=b'{"data": "ok"}'),
        ]
        with patch("mcp_zuul.helpers.kerberos_auth", new_callable=AsyncMock) as mock_auth:
            resp = await fetch_log_url(a, url)

        assert resp.status_code == 200
        mock_auth.assert_awaited_once()
        assert a._auth_generation == 1

    @respx.mock
    async def test_fetch_log_no_reauth_cross_origin(self, krb_ctx):
        """401 from different host (log host) → no re-auth, just return 401."""
        a = krb_ctx.request_context.lifespan_context
        # Log URL on a DIFFERENT host than the API
        url = "https://logs.external.com/build/job-output.json"

        respx.get(url).mock(return_value=httpx.Response(401, text="Unauthorized"))
        with patch("mcp_zuul.helpers.kerberos_auth", new_callable=AsyncMock) as mock_auth:
            resp = await fetch_log_url(a, url)

        # No re-auth — cross-origin logs use log_client (no auth)
        assert resp.status_code == 401
        mock_auth.assert_not_awaited()
        assert a._auth_generation == 0

    @respx.mock
    async def test_stream_log_reauths_same_origin(self, krb_ctx):
        """stream_log 401 from API host → re-auth → retry → 200."""
        a = krb_ctx.request_context.lifespan_context
        url = "https://zuul.example.com/logs/build/console.log"

        route = respx.get(url)
        route.side_effect = [
            httpx.Response(401, text="Unauthorized"),
            httpx.Response(200, content=b"log content here"),
        ]
        with patch("mcp_zuul.helpers.kerberos_auth", new_callable=AsyncMock) as mock_auth:
            content, truncated = await stream_log(a, url)

        assert content == b"log content here"
        assert truncated is False
        mock_auth.assert_awaited_once()

    @respx.mock
    async def test_stream_log_404_after_reauth(self, krb_ctx):
        """If log returns 404 after re-auth, raise FileNotFoundError."""
        a = krb_ctx.request_context.lifespan_context
        url = "https://zuul.example.com/logs/build/missing.log"

        respx.get(url).mock(return_value=httpx.Response(404, text="Not Found"))
        # 404 doesn't trigger re-auth (code checks status != 404 before streaming)
        with (
            patch("mcp_zuul.helpers.kerberos_auth", new_callable=AsyncMock) as mock_auth,
            pytest.raises(FileNotFoundError),
        ):
            await stream_log(a, url)
        mock_auth.assert_not_awaited()

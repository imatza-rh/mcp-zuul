"""Tests for Kerberos/SPNEGO authentication."""

import base64
import sys
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from mcp_zuul.auth import _follow_redirect


class TestFollowRedirect:
    def test_returns_location_for_302(self):
        resp = httpx.Response(302, headers={"location": "https://sso.example.com/login"})
        assert _follow_redirect(resp) == "https://sso.example.com/login"

    def test_returns_location_for_301(self):
        resp = httpx.Response(301, headers={"location": "https://new.example.com/"})
        assert _follow_redirect(resp) == "https://new.example.com/"

    def test_returns_location_for_307(self):
        resp = httpx.Response(307, headers={"location": "https://temp.example.com/"})
        assert _follow_redirect(resp) == "https://temp.example.com/"

    def test_returns_location_for_308(self):
        resp = httpx.Response(308, headers={"location": "https://perm.example.com/"})
        assert _follow_redirect(resp) == "https://perm.example.com/"

    def test_returns_none_for_200(self):
        resp = httpx.Response(200)
        assert _follow_redirect(resp) is None

    def test_returns_none_for_404(self):
        resp = httpx.Response(404)
        assert _follow_redirect(resp) is None

    def test_returns_none_for_401(self):
        resp = httpx.Response(401)
        assert _follow_redirect(resp) is None

    def test_raises_when_no_location_header(self):
        resp = httpx.Response(302, headers={})
        with pytest.raises(RuntimeError, match="no Location header"):
            _follow_redirect(resp)


@pytest.fixture
def mock_gssapi():
    """Inject a mock gssapi module into sys.modules."""
    mock_mod = MagicMock()
    mock_mod.NameType.hostbased_service = "hostbased"
    mock_mod.exceptions.GSSError = type("GSSError", (Exception,), {})
    original = sys.modules.get("gssapi")
    sys.modules["gssapi"] = mock_mod
    yield mock_mod
    if original is not None:
        sys.modules["gssapi"] = original
    else:
        del sys.modules["gssapi"]


class TestKerberosAuth:
    async def test_successful_auth(self, mock_gssapi):
        from mcp_zuul.auth import kerberos_auth

        mock_ctx = MagicMock()
        mock_ctx.step.return_value = b"spnego-token-bytes"
        mock_gssapi.SecurityContext.return_value = mock_ctx

        client = AsyncMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(
            side_effect=[
                httpx.Response(302, headers={"location": "https://sso.example.com/auth"}),
                httpx.Response(401, headers={"www-authenticate": "Negotiate"}),
                httpx.Response(
                    302,
                    headers={"location": "https://zuul.example.com/callback?code=abc"},
                ),
                httpx.Response(200),
            ]
        )

        await kerberos_auth(client, "https://zuul.example.com")

        # Verify SPNEGO token was sent in the auth request
        calls = client.get.call_args_list
        auth_call = calls[2]
        auth_header = auth_call.kwargs.get("headers", {}).get("Authorization", "")
        expected_token = base64.b64encode(b"spnego-token-bytes").decode()
        assert auth_header == f"Negotiate {expected_token}"

    async def test_200_treated_as_already_authed(self, mock_gssapi):
        """If server returns 200 (session valid after cookie clear), accept it."""
        from mcp_zuul.auth import kerberos_auth

        client = AsyncMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(return_value=httpx.Response(200))

        await kerberos_auth(client, "https://zuul.example.com")

    async def test_unexpected_status_raises(self, mock_gssapi):
        """Non-200, non-401 status raises RuntimeError."""
        from mcp_zuul.auth import kerberos_auth

        client = AsyncMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(return_value=httpx.Response(403))

        with pytest.raises(RuntimeError, match=r"expected 401 Negotiate.*got 403"):
            await kerberos_auth(client, "https://zuul.example.com")

    async def test_wrong_auth_scheme(self, mock_gssapi):
        from mcp_zuul.auth import kerberos_auth

        client = AsyncMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(
            return_value=httpx.Response(401, headers={"www-authenticate": "Basic realm=test"})
        )

        with pytest.raises(RuntimeError, match="did not offer Negotiate"):
            await kerberos_auth(client, "https://zuul.example.com")

    async def test_spnego_failure(self, mock_gssapi):
        from mcp_zuul.auth import kerberos_auth

        mock_ctx = MagicMock()
        mock_ctx.step.side_effect = mock_gssapi.exceptions.GSSError("no ticket")
        mock_gssapi.SecurityContext.return_value = mock_ctx

        client = AsyncMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(
            return_value=httpx.Response(401, headers={"www-authenticate": "Negotiate"})
        )

        with pytest.raises(RuntimeError, match="SPNEGO token generation failed"):
            await kerberos_auth(client, "https://zuul.example.com")

    async def test_final_response_not_200(self, mock_gssapi):
        from mcp_zuul.auth import kerberos_auth

        mock_ctx = MagicMock()
        mock_ctx.step.return_value = b"token"
        mock_gssapi.SecurityContext.return_value = mock_ctx

        client = AsyncMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(
            side_effect=[
                httpx.Response(401, headers={"www-authenticate": "Negotiate"}),
                httpx.Response(403),
            ]
        )

        with pytest.raises(RuntimeError, match="final response was 403"):
            await kerberos_auth(client, "https://zuul.example.com")

    async def test_clears_cookies_before_auth(self, mock_gssapi):
        """Stale session cookies are cleared so the OIDC chain starts fresh."""
        from mcp_zuul.auth import kerberos_auth

        mock_ctx = MagicMock()
        mock_ctx.step.return_value = b"token"
        mock_gssapi.SecurityContext.return_value = mock_ctx

        client = AsyncMock(spec=httpx.AsyncClient)
        client.cookies = MagicMock()
        client.get = AsyncMock(
            side_effect=[
                httpx.Response(401, headers={"www-authenticate": "Negotiate"}),
                httpx.Response(200),
            ]
        )

        await kerberos_auth(client, "https://zuul.example.com")
        client.cookies.clear.assert_called_once()

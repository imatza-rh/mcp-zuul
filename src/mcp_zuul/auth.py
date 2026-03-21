"""Kerberos / SPNEGO authentication for Zuul MCP server."""

import base64
import logging
from urllib.parse import urlparse

import httpx

log = logging.getLogger("zuul-mcp")


def _follow_redirect(resp: httpx.Response) -> str | None:
    """Extract the Location header from a redirect response."""
    if resp.status_code not in (301, 302, 303, 307, 308):
        return None
    location = resp.headers.get("location")
    if not location:
        raise RuntimeError(f"Kerberos auth: {resp.status_code} redirect has no Location header")
    return location


async def kerberos_auth(client: httpx.AsyncClient, base_url: str) -> None:
    """Authenticate via SPNEGO/Kerberos against an OIDC-protected Zuul.

    Drives the redirect chain manually:
      Zuul API -> 302 OIDC login -> 401 Negotiate -> SPNEGO token ->
      302 callback -> session cookie established.

    Requires a valid Kerberos ticket (run ``kinit`` first).
    """
    import gssapi

    max_hops = 10
    url = f"{base_url}/api/tenants"

    # The client may have Accept: application/json which causes some servers
    # to return 401 directly instead of redirecting to SSO.  Override with
    # a browser-like Accept during the auth handshake.
    auth_headers: dict[str, str] = {"Accept": "text/html"}

    # Follow redirects until we hit a 401 Negotiate challenge.
    resp = await client.get(url, headers=auth_headers, follow_redirects=False)
    for _ in range(max_hops):
        location = _follow_redirect(resp)
        if location:
            url = location
            resp = await client.get(url, headers=auth_headers, follow_redirects=False)
        else:
            break

    if resp.status_code != 401:
        raise RuntimeError(
            f"Kerberos auth: expected 401 Negotiate challenge, got {resp.status_code}"
        )
    www_auth = resp.headers.get("www-authenticate", "")
    if "negotiate" not in www_auth.lower():
        raise RuntimeError(f"Kerberos auth: server did not offer Negotiate (got: {www_auth})")

    # Generate SPNEGO token for the SSO host.
    host = urlparse(url).hostname
    spn = gssapi.Name(f"HTTP@{host}", gssapi.NameType.hostbased_service)
    ctx = gssapi.SecurityContext(name=spn, usage="initiate")

    # Extract server token from "Negotiate <base64>" if present.
    in_token = None
    parts = www_auth.strip().split()
    if len(parts) >= 2 and parts[0].lower() == "negotiate":
        in_token = base64.b64decode(parts[1])

    try:
        out_token = ctx.step(in_token)
    except gssapi.exceptions.GSSError as e:
        raise RuntimeError(
            f"Kerberos auth: SPNEGO token generation failed (is your ticket valid? run kinit): {e}"
        ) from e

    # Send the authenticated request to the SSO endpoint.
    resp = await client.get(
        url,
        headers={"Authorization": f"Negotiate {base64.b64encode(out_token).decode()}"},
        follow_redirects=False,
    )

    # Follow remaining redirects (SSO callback -> Zuul session).
    for _ in range(max_hops):
        location = _follow_redirect(resp)
        if location:
            resp = await client.get(location, follow_redirects=False)
        else:
            break

    if resp.status_code != 200:
        raise RuntimeError(f"Kerberos auth: final response was {resp.status_code}, expected 200")
    log.info("Kerberos authentication successful")

"""Kerberos / SPNEGO authentication for Zuul MCP server."""

import base64
import logging
import secrets
from urllib.parse import parse_qs, urlencode, urlparse

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


def _extract_oidc_params(url: str) -> tuple[str, str, str, str] | None:
    """Extract OIDC params from an authorize URL.

    Returns (client_id, redirect_uri, token_url, authorize_base) or None.
    """
    parsed = urlparse(url)
    if "/openid-connect/auth" not in parsed.path:
        return None
    qs = parse_qs(parsed.query)
    client_id = qs.get("client_id", [None])[0]
    redirect_uri = qs.get("redirect_uri", [None])[0]
    if not client_id or not redirect_uri:
        return None
    realm_base = url.split("/protocol/")[0]
    token_url = realm_base + "/protocol/openid-connect/token"
    authorize_url = realm_base + "/protocol/openid-connect/auth"
    return client_id, redirect_uri, token_url, authorize_url


async def kerberos_auth(client: httpx.AsyncClient, base_url: str) -> None:
    """Authenticate via SPNEGO/Kerberos against an OIDC-protected Zuul.

    Two-phase flow:
      1. Session establishment: OIDC redirect chain → SPNEGO → callback → session cookie.
      2. JWT acquisition: direct OIDC authorize request → SSO auto-authenticates →
         intercept auth code → exchange for JWT at token endpoint.

    Phase 2 goes directly to the SSO (not through the reverse proxy), so the
    session cookies from phase 1 are preserved.

    Requires a valid Kerberos ticket (run ``kinit`` first).
    """
    import gssapi

    max_hops = 10
    url = f"{base_url}/api/tenants"

    # Clear stale session cookies so the OIDC redirect chain starts fresh.
    client.cookies.clear()

    auth_headers: dict[str, str] = {"Accept": "text/html"}
    oidc_params: tuple[str, str, str, str] | None = None

    # Follow redirects until we hit a 401 Negotiate challenge.
    resp = await client.get(url, headers=auth_headers, follow_redirects=False)
    for _ in range(max_hops):
        location = _follow_redirect(resp)
        if location:
            if not oidc_params:
                oidc_params = _extract_oidc_params(location)
            url = location
            resp = await client.get(url, headers=auth_headers, follow_redirects=False)
        else:
            break

    if resp.status_code == 200:
        log.info("Kerberos auth: session still valid after cookie clear")
        if oidc_params:
            await _acquire_admin_jwt(client, *oidc_params)
        return
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

    if not out_token:
        raise RuntimeError("Kerberos auth: SPNEGO context produced no token")

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
    log.info("Kerberos authentication successful (session established)")

    # Phase 2: acquire JWT for admin API endpoints.
    if oidc_params:
        await _acquire_admin_jwt(client, *oidc_params)


async def _acquire_admin_jwt(
    client: httpx.AsyncClient,
    client_id: str,
    redirect_uri: str,
    token_url: str,
    authorize_url: str,
) -> None:
    """Acquire a JWT by hitting the OIDC authorize URL directly.

    Goes straight to the SSO (not through the reverse proxy), so the
    Zuul session cookies from phase 1 are untouched. The SSO auto-
    authenticates using the Kerberos ticket and redirects to the callback
    with a fresh auth code. We intercept the code and exchange it for a JWT.
    """
    import gssapi

    # Build a fresh OIDC authorize URL with new state/nonce.
    params = {
        "response_type": "code",
        "scope": "openid profile roles",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": secrets.token_urlsafe(24),
        "nonce": secrets.token_urlsafe(24),
    }
    url = f"{authorize_url}?{urlencode(params)}"

    # Hit the SSO directly — it should auto-authenticate (Kerberos session
    # is still valid) and redirect to the callback with an auth code.
    max_hops = 10
    code = None
    resp = await client.get(url, follow_redirects=False)
    for _ in range(max_hops):
        location = _follow_redirect(resp)
        if not location:
            break
        if "code=" in location:
            code = parse_qs(urlparse(location).query).get("code", [None])[0]
            if code:
                break
        # If SSO needs Kerberos negotiate again, handle it.
        resp = await client.get(location, follow_redirects=False)
        if resp.status_code == 401:
            www_auth = resp.headers.get("www-authenticate", "")
            if "negotiate" in www_auth.lower():
                host = urlparse(location).hostname
                spn = gssapi.Name(f"HTTP@{host}", gssapi.NameType.hostbased_service)
                ctx = gssapi.SecurityContext(name=spn, usage="initiate")
                in_token = None
                parts = www_auth.strip().split()
                if len(parts) >= 2 and parts[0].lower() == "negotiate":
                    in_token = base64.b64decode(parts[1])
                try:
                    out_token = ctx.step(in_token)
                except gssapi.exceptions.GSSError:
                    log.warning("JWT acquisition: SPNEGO re-negotiate failed")
                    return
                if out_token:
                    resp = await client.get(
                        location,
                        headers={
                            "Authorization": f"Negotiate {base64.b64encode(out_token).decode()}"
                        },
                        follow_redirects=False,
                    )

    if not code:
        log.warning("JWT acquisition: no auth code captured, admin API may not work")
        return

    # Exchange the code for a JWT (public client, no client_secret).
    token_resp = await client.post(
        token_url,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
        },
        follow_redirects=False,
    )

    if token_resp.status_code != 200:
        log.warning("JWT acquisition: token endpoint returned %d", token_resp.status_code)
        return

    try:
        token_data = token_resp.json()
    except Exception:
        log.warning("JWT acquisition: token endpoint returned non-JSON")
        return

    access_token = token_data.get("access_token")
    if not access_token:
        log.warning("JWT acquisition: no access_token in response")
        return

    client.headers["authorization"] = f"Bearer {access_token}"
    expires_in = token_data.get("expires_in", "?")
    log.info("JWT acquired for admin API (expires in %ss)", expires_in)

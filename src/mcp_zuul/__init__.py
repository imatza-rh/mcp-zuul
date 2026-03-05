"""Zuul CI MCP Server — read-only access to builds, logs, status, and jobs."""

import asyncio
import base64
import functools
import json
import os
import re
import sys
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlparse

import httpx
from mcp.server.fastmcp import FastMCP, Context

# ---------------------------------------------------------------------------
# Logging (stderr only — mandatory for stdio transport)
# ---------------------------------------------------------------------------
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("zuul-mcp")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class Config:
    base_url: str
    default_tenant: str
    auth_token: str | None
    timeout: int
    verify_ssl: bool
    use_kerberos: bool

    @classmethod
    def from_env(cls) -> "Config":
        base_url = os.environ.get("ZUUL_URL", "").rstrip("/")
        if not base_url:
            log.error("ZUUL_URL environment variable is required")
            sys.exit(1)
        raw_timeout = os.environ.get("ZUUL_TIMEOUT", "30")
        try:
            timeout = int(raw_timeout)
        except ValueError:
            log.error("ZUUL_TIMEOUT must be an integer (seconds), got: %s", raw_timeout)
            sys.exit(1)
        use_kerberos = os.environ.get("ZUUL_USE_KERBEROS", "false").lower() == "true"
        auth_token = os.environ.get("ZUUL_AUTH_TOKEN")
        if use_kerberos and auth_token:
            log.error("ZUUL_USE_KERBEROS and ZUUL_AUTH_TOKEN are mutually exclusive")
            sys.exit(1)
        if use_kerberos:
            try:
                import gssapi  # noqa: F401
            except ImportError:
                log.error(
                    "ZUUL_USE_KERBEROS=true but 'gssapi' is not installed. "
                    "Install with: pip install mcp-zuul[kerberos]"
                )
                sys.exit(1)
        return cls(
            base_url=base_url,
            default_tenant=os.environ.get("ZUUL_DEFAULT_TENANT", ""),
            auth_token=auth_token,
            timeout=timeout,
            verify_ssl=os.environ.get("ZUUL_VERIFY_SSL", "true").lower() == "true",
            use_kerberos=use_kerberos,
        )


# ---------------------------------------------------------------------------
# Kerberos / SPNEGO authentication
# ---------------------------------------------------------------------------

def _follow_redirect(resp: httpx.Response) -> str | None:
    """Extract the Location header from a redirect response."""
    if resp.status_code not in (301, 302, 303, 307, 308):
        return None
    location = resp.headers.get("location")
    if not location:
        raise RuntimeError(
            f"Kerberos auth: {resp.status_code} redirect has no Location header"
        )
    return location


async def _kerberos_auth(client: httpx.AsyncClient, base_url: str) -> None:
    """Authenticate via SPNEGO/Kerberos against an OIDC-protected Zuul.

    Drives the redirect chain manually:
      Zuul API → 302 OIDC login → 401 Negotiate → SPNEGO token →
      302 callback → session cookie established.

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
        raise RuntimeError(
            f"Kerberos auth: server did not offer Negotiate (got: {www_auth})"
        )

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
            f"Kerberos auth: SPNEGO token generation failed "
            f"(is your ticket valid? run kinit): {e}"
        ) from e

    # Send the authenticated request to the SSO endpoint.
    resp = await client.get(
        url,
        headers={"Authorization": f"Negotiate {base64.b64encode(out_token).decode()}"},
        follow_redirects=False,
    )

    # Follow remaining redirects (SSO callback → Zuul session).
    for _ in range(max_hops):
        location = _follow_redirect(resp)
        if location:
            resp = await client.get(location, follow_redirects=False)
        else:
            break

    if resp.status_code != 200:
        raise RuntimeError(
            f"Kerberos auth: final response was {resp.status_code}, expected 200"
        )
    log.info("Kerberos authentication successful")


# ---------------------------------------------------------------------------
# Lifespan — httpx.AsyncClient lifecycle
# ---------------------------------------------------------------------------

@dataclass
class AppContext:
    client: httpx.AsyncClient
    log_client: httpx.AsyncClient
    config: Config


@asynccontextmanager
async def lifespan(server: FastMCP):
    config = Config.from_env()
    headers = {"Accept": "application/json"}
    if config.auth_token:
        headers["Authorization"] = f"Bearer {config.auth_token}"
    async with httpx.AsyncClient(
        base_url=config.base_url,
        headers=headers,
        timeout=config.timeout,
        follow_redirects=True,
        verify=config.verify_ssl,
    ) as client, httpx.AsyncClient(
        timeout=config.timeout,
        follow_redirects=True,
        verify=config.verify_ssl,
    ) as log_client:
        if config.use_kerberos:
            await _kerberos_auth(client, config.base_url)
        log.info("Zuul MCP connected to %s", config.base_url)
        yield AppContext(client=client, log_client=log_client, config=config)


mcp = FastMCP("zuul", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _app(ctx: Context) -> AppContext:
    return ctx.request_context.lifespan_context


def _tenant(ctx: Context, tenant: str) -> str:
    t = tenant or _app(ctx).config.default_tenant
    if not t:
        raise ValueError("tenant is required (no ZUUL_DEFAULT_TENANT set)")
    return t


def _safepath(value: str) -> str:
    """Sanitize a user-supplied value for use in a URL path.

    Preserves slashes (needed for Zuul project names like org/repo)
    but rejects path traversal attempts.
    """
    if ".." in value.split("/"):
        raise ValueError(f"Invalid path segment: {value!r}")
    return quote(value, safe="/")


async def _api(ctx: Context, path: str, params: dict | None = None) -> Any:
    app = _app(ctx)
    resp = await app.client.get(f"/api{path}", params=params)

    # Re-authenticate if the session expired (Kerberos only).
    if resp.status_code in (401, 302) and app.config.use_kerberos:
        log.info("Session expired, re-authenticating via Kerberos")
        await _kerberos_auth(app.client, app.config.base_url)
        resp = await app.client.get(f"/api{path}", params=params)

    resp.raise_for_status()
    return resp.json()


def _error(msg: str) -> str:
    return json.dumps({"error": msg})


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _clean(d: dict) -> dict:
    """Remove None values to save tokens."""
    return {k: v for k, v in d.items() if v is not None}


# ---------------------------------------------------------------------------
# Formatters — compact, token-efficient representations
# ---------------------------------------------------------------------------

def _fmt_build(b: dict, brief: bool = True) -> dict:
    out = {
        "uuid": b.get("uuid", "unknown"),
        "job": b["job_name"],
        "result": b.get("result") or "IN_PROGRESS",
        "pipeline": b.get("pipeline", ""),
        "duration": b.get("duration"),
        "voting": b.get("voting", True),
    }
    ref = b.get("ref") or {}
    if ref:
        out["project"] = ref.get("project", "")
        out["change"] = ref.get("change")
        out["ref_url"] = ref.get("ref_url", "")
    if not brief:
        out["start_time"] = b.get("start_time")
        out["end_time"] = b.get("end_time")
        out["log_url"] = b.get("log_url")
        out["nodeset"] = b.get("nodeset")
        out["error_detail"] = b.get("error_detail")
        out["artifacts"] = [a["name"] for a in b.get("artifacts", [])]
        out["patchset"] = ref.get("patchset")
        out["branch"] = ref.get("branch")
        bs = b.get("buildset")
        if bs:
            out["buildset_uuid"] = bs.get("uuid")
    return _clean(out)


def _fmt_buildset(bs: dict, brief: bool = True) -> dict:
    out = {
        "uuid": bs.get("uuid", "unknown"),
        "result": bs.get("result") or "IN_PROGRESS",
        "pipeline": bs.get("pipeline", ""),
        "event_timestamp": bs.get("event_timestamp"),
    }
    refs = bs.get("refs", [])
    if refs:
        r = refs[0]
        out["project"] = r.get("project", "")
        out["change"] = r.get("change")
        out["ref_url"] = r.get("ref_url", "")
    if not brief:
        out["message"] = bs.get("message")
        out["first_build_start"] = bs.get("first_build_start_time")
        out["last_build_end"] = bs.get("last_build_end_time")
        if "builds" in bs:
            out["builds"] = [_fmt_build(b) for b in bs["builds"]]
        if "events" in bs:
            out["events"] = bs["events"]
    return _clean(out)


def _fmt_status_item(item: dict) -> dict:
    out = {
        "id": item.get("id", ""),
        "active": item.get("active", False),
        "live": item.get("live", False),
    }
    refs = item.get("refs", [])
    if refs:
        r = refs[0]
        out["project"] = r.get("project", "")
        out["change"] = r.get("change") or r.get("ref", "")
        out["url"] = r.get("url", "")
    jobs = item.get("jobs", [])
    if jobs:
        out["jobs"] = [
            _clean({
                "name": j.get("name", ""),
                "result": j.get("result"),
                "voting": j.get("voting", True),
                "elapsed": j.get("elapsed_time"),
                "remaining": j.get("remaining_time"),
            })
            for j in jobs
        ]
    failing = item.get("failing_reasons", [])
    if failing:
        out["failing_reasons"] = failing
    return out


# ---------------------------------------------------------------------------
# Error handling decorator
# ---------------------------------------------------------------------------

def _handle_errors(func):
    """Wrap tool functions with uniform error handling."""
    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except httpx.HTTPStatusError as e:
            body = e.response.text[:200] if e.response.text else ""
            return _error(f"API returned {e.response.status_code}: {body}")
        except httpx.ConnectError:
            return _error("Cannot connect to Zuul API")
        except httpx.TimeoutException:
            return _error("Request timed out")
        except ValueError as e:
            return _error(str(e))
        except Exception as e:
            log.exception("Unexpected error in %s", func.__name__)
            return _error(f"Internal error: {type(e).__name__}: {e}")
    return wrapper


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
@_handle_errors
async def list_tenants(ctx: Context) -> str:
    """List all Zuul tenants with project counts."""
    data = await _api(ctx, "/tenants")
    result = [
        _clean({"name": t["name"], "projects": t.get("projects", 0), "queue": t.get("queue", 0)})
        for t in data
    ]
    return json.dumps(result)


@mcp.tool()
@_handle_errors
async def get_status(
    ctx: Context,
    tenant: str = "",
    pipeline: str = "",
    project: str = "",
    active_only: bool = True,
) -> str:
    """Live pipeline status showing what's currently queued/running.

    Args:
        tenant: Tenant name (uses default if empty)
        pipeline: Filter to a specific pipeline name
        project: Filter to a specific project
        active_only: Only show pipelines with active items (default true)
    """
    t = _tenant(ctx, tenant)
    data = await _api(ctx, f"/tenant/{_safepath(t)}/status")

    pipelines = data.get("pipelines", [])
    result = []
    for p in pipelines:
        if pipeline and p["name"] != pipeline:
            continue
        items = []
        for queue in p.get("change_queues", []):
            for heads_group in queue.get("heads", []):
                for item in heads_group:
                    if project:
                        item_projects = [
                            r.get("project", "") for r in item.get("refs", [])
                        ]
                        if not any(project in proj for proj in item_projects):
                            continue
                    if active_only and not item.get("active", False):
                        continue
                    items.append(_fmt_status_item(item))
                    if len(items) >= 50:
                        break
                if len(items) >= 50:
                    break
            if len(items) >= 50:
                break
        if items or not active_only:
            result.append({
                "pipeline": p["name"],
                "item_count": len(items),
                "items": items,
            })

    # Only include pipelines with items when active_only
    if active_only:
        result = [r for r in result if r["item_count"] > 0]

    return json.dumps({
        "zuul_version": data.get("zuul_version"),
        "pipeline_count": len(result),
        "pipelines": result,
    })


@mcp.tool()
@_handle_errors
async def get_change_status(
    ctx: Context,
    change: str,
    tenant: str = "",
) -> str:
    """Pipeline status for a specific Gerrit change or GitHub PR.

    Args:
        change: Change number (e.g. "12345")
        tenant: Tenant name (uses default if empty)
    """
    t = _tenant(ctx, tenant)
    data = await _api(ctx, f"/tenant/{_safepath(t)}/status/change/{_safepath(change)}")
    if not data:
        return json.dumps({"change": change, "status": "not_in_pipeline"})
    return json.dumps([_fmt_status_item(item) for item in data])


@mcp.tool()
@_handle_errors
async def list_builds(
    ctx: Context,
    tenant: str = "",
    project: str = "",
    pipeline: str = "",
    job_name: str = "",
    change: str = "",
    branch: str = "",
    patchset: str = "",
    ref: str = "",
    result: str = "",
    limit: int = 20,
    skip: int = 0,
) -> str:
    """Search builds with filters. Returns compact build summaries.

    Args:
        tenant: Tenant name (uses default if empty)
        project: Filter by project name
        pipeline: Filter by pipeline name
        job_name: Filter by job name
        change: Filter by change number
        branch: Filter by branch name
        patchset: Filter by patchset
        ref: Filter by git ref
        result: Filter by result (SUCCESS, FAILURE, TIMED_OUT, SKIPPED, etc.)
        limit: Max results, 1-100 (default 20)
        skip: Offset for pagination (default 0)
    """
    t = _tenant(ctx, tenant)
    limit = max(1, min(limit, 100))
    params: dict[str, Any] = {"limit": limit + 1, "skip": skip}
    for key, val in [
        ("project", project), ("pipeline", pipeline), ("job_name", job_name),
        ("change", change), ("branch", branch), ("patchset", patchset),
        ("ref", ref), ("result", result),
    ]:
        if val:
            params[key] = val

    data = await _api(ctx, f"/tenant/{_safepath(t)}/builds", params)
    has_more = len(data) > limit
    builds = [_fmt_build(b) for b in data[:limit]]
    return json.dumps({"builds": builds, "count": len(builds),
                        "has_more": has_more, "skip": skip})


@mcp.tool()
@_handle_errors
async def get_build(
    ctx: Context,
    uuid: str,
    tenant: str = "",
) -> str:
    """Get full details for a single build by UUID.

    Args:
        uuid: Build UUID (full or prefix from list_builds)
        tenant: Tenant name (uses default if empty)
    """
    t = _tenant(ctx, tenant)
    data = await _api(ctx, f"/tenant/{_safepath(t)}/build/{_safepath(uuid)}")
    return json.dumps(_fmt_build(data, brief=False))


# Log fetching constants
_MAX_LOG_LINES = 200
_MAX_LOG_BYTES = 10 * 1024 * 1024  # 10 MB
_ERROR_PATTERNS = re.compile(
    r"(FAILED|ERROR|UNREACHABLE|fatal|Traceback|TASK \[.*\] \*+$)",
    re.IGNORECASE,
)


@mcp.tool()
@_handle_errors
async def get_build_log(
    ctx: Context,
    uuid: str,
    tenant: str = "",
    mode: str = "summary",
    lines: int = 0,
    grep: str = "",
) -> str:
    """Fetch and parse build log (job-output.txt).

    Args:
        uuid: Build UUID
        tenant: Tenant name (uses default if empty)
        mode: "summary" (default: tail + error lines) or "full" (paginated chunks)
        lines: For summary: tail line count (default 100). For full: offset start line.
        grep: Regex pattern to filter log lines (overrides mode)
    """
    t = _tenant(ctx, tenant)
    build = await _api(ctx, f"/tenant/{_safepath(t)}/build/{_safepath(uuid)}")
    log_url = build.get("log_url")
    if not log_url:
        return _error(f"No log_url for build {uuid}")

    # Validate log URL scheme before fetching
    parsed = urlparse(log_url)
    if parsed.scheme not in ("http", "https"):
        return _error(f"Invalid log URL scheme: {parsed.scheme}")

    # Fetch job-output.txt (separate client — no auth token)
    app = _app(ctx)
    txt_url = log_url.rstrip("/") + "/job-output.txt"
    chunks: list[bytes] = []
    size = 0
    async with app.log_client.stream("GET", txt_url) as resp:
        if resp.status_code == 404:
            return _error(f"job-output.txt not found at {txt_url}")
        resp.raise_for_status()
        async for chunk in resp.aiter_bytes():
            size += len(chunk)
            if size > _MAX_LOG_BYTES:
                break
            chunks.append(chunk)

    raw = _strip_ansi(b"".join(chunks).decode("utf-8", errors="replace"))
    all_lines = raw.splitlines()
    total = len(all_lines)

    # Grep mode
    if grep:
        try:
            pat = re.compile(grep, re.IGNORECASE)
        except re.error as e:
            return _error(f"Invalid regex: {e}")
        try:
            matched = await asyncio.wait_for(
                asyncio.get_running_loop().run_in_executor(
                    None,
                    lambda: [(i + 1, l) for i, l in enumerate(all_lines) if pat.search(l)],
                ),
                timeout=10.0,
            )
        except asyncio.TimeoutError:
            return _error("Regex search timed out (pattern may be too complex)")
        return json.dumps({
            "total_lines": total,
            "log_url": txt_url,
            "grep": grep,
            "matched": len(matched),
            "lines": [{"n": n, "text": text[:500]} for n, text in matched[:100]],
        })

    # Summary mode — single pass for both errors and tail
    if mode == "summary":
        tail_n = lines or 100
        tail_start = max(0, total - tail_n)
        errors = []
        tail = []
        for i, line in enumerate(all_lines):
            if _ERROR_PATTERNS.search(line) and len(errors) < 30:
                errors.append((i + 1, line))
            if i >= tail_start:
                tail.append(line)
        return json.dumps({
            "total_lines": total,
            "log_url": txt_url,
            "job": build.get("job_name", ""),
            "result": build.get("result", ""),
            "error_lines": [{"n": n, "text": t[:500]} for n, t in errors],
            "tail": [l[:500] for l in tail],
        })

    # Full mode (paginated)
    offset = lines or 0
    chunk = all_lines[offset : offset + _MAX_LOG_LINES]
    return json.dumps({
        "total_lines": total,
        "log_url": txt_url,
        "offset": offset,
        "count": len(chunk),
        "has_more": offset + len(chunk) < total,
        "lines": [l[:500] for l in chunk],
    })


@mcp.tool()
@_handle_errors
async def list_buildsets(
    ctx: Context,
    tenant: str = "",
    project: str = "",
    pipeline: str = "",
    change: str = "",
    branch: str = "",
    ref: str = "",
    result: str = "",
    limit: int = 20,
    skip: int = 0,
) -> str:
    """Search buildsets (groups of builds triggered by a single event).

    Args:
        tenant: Tenant name (uses default if empty)
        project: Filter by project
        pipeline: Filter by pipeline name
        change: Filter by change number
        branch: Filter by branch name
        ref: Filter by git ref
        result: Filter by result
        limit: Max results, 1-100 (default 20)
        skip: Offset for pagination
    """
    t = _tenant(ctx, tenant)
    limit = max(1, min(limit, 100))
    params: dict[str, Any] = {"limit": limit + 1, "skip": skip}
    for key, val in [
        ("project", project), ("pipeline", pipeline), ("change", change),
        ("branch", branch), ("ref", ref), ("result", result),
    ]:
        if val:
            params[key] = val

    data = await _api(ctx, f"/tenant/{_safepath(t)}/buildsets", params)
    has_more = len(data) > limit
    buildsets = [_fmt_buildset(bs) for bs in data[:limit]]
    return json.dumps({
        "buildsets": buildsets,
        "count": len(buildsets),
        "has_more": has_more,
        "skip": skip,
    })


@mcp.tool()
@_handle_errors
async def get_buildset(
    ctx: Context,
    uuid: str,
    tenant: str = "",
) -> str:
    """Get buildset details including all builds and events.

    Args:
        uuid: Buildset UUID
        tenant: Tenant name (uses default if empty)
    """
    t = _tenant(ctx, tenant)
    data = await _api(ctx, f"/tenant/{_safepath(t)}/buildset/{_safepath(uuid)}")
    return json.dumps(_fmt_buildset(data, brief=False))


@mcp.tool()
@_handle_errors
async def list_jobs(
    ctx: Context,
    tenant: str = "",
    filter: str = "",
) -> str:
    """List all jobs in a tenant. Optionally filter by name substring.

    Args:
        tenant: Tenant name (uses default if empty)
        filter: Case-insensitive substring to filter job names
    """
    t = _tenant(ctx, tenant)
    data = await _api(ctx, f"/tenant/{_safepath(t)}/jobs")
    if filter:
        f_lower = filter.lower()
        data = [j for j in data if f_lower in j.get("name", "").lower()]
    result = [
        _clean({
            "name": j["name"],
            "description": (j.get("description") or "")[:100] or None,
            "variants": len(j.get("variants", [])),
        })
        for j in data
    ]
    return json.dumps({"jobs": result, "count": len(result)})


@mcp.tool()
@_handle_errors
async def get_job(
    ctx: Context,
    name: str,
    tenant: str = "",
) -> str:
    """Get job configuration and variants.

    Args:
        name: Job name
        tenant: Tenant name (uses default if empty)
    """
    t = _tenant(ctx, tenant)
    data = await _api(ctx, f"/tenant/{_safepath(t)}/job/{_safepath(name)}")
    variants = []
    for v in data:
        sc = v.get("source_context") or {}
        compact = {
            "parent": v.get("parent"),
            "branches": v.get("branches", []) or None,
            "nodeset": v.get("nodeset"),
            "timeout": v.get("timeout"),
            "voting": v.get("voting", True),
            "abstract": v.get("abstract", False) or None,
            "description": (v.get("description") or "")[:200] or None,
            "source_project": sc.get("project"),
        }
        variants.append(_clean(compact))
    return json.dumps({"name": name, "variants": variants})


@mcp.tool()
@_handle_errors
async def get_project(
    ctx: Context,
    name: str,
    tenant: str = "",
) -> str:
    """Get project configuration — which pipelines and jobs are configured.

    Args:
        name: Project name (e.g. "openstack-k8s-operators/openstack-operator")
        tenant: Tenant name (uses default if empty)
    """
    t = _tenant(ctx, tenant)
    data = await _api(ctx, f"/tenant/{_safepath(t)}/project/{_safepath(name)}")
    configs: dict[str, list[str]] = {}
    for cfg in data.get("configs", []):
        for pipeline in cfg.get("pipelines", []):
            pname = pipeline.get("name", "")
            jobs = []
            for j in pipeline.get("jobs", []):
                if isinstance(j, list):
                    jobs.append(j[0]["name"] if j else "")
                elif isinstance(j, dict):
                    jobs.append(j.get("name", ""))
            if jobs:
                configs[pname] = jobs
    return json.dumps(_clean({
        "project": name,
        "canonical_name": data.get("canonical_name"),
        "connection": data.get("connection_name"),
        "type": data.get("type"),
        "pipelines": configs,
    }))


@mcp.tool()
@_handle_errors
async def list_pipelines(
    ctx: Context,
    tenant: str = "",
) -> str:
    """List all pipelines with their trigger types.

    Args:
        tenant: Tenant name (uses default if empty)
    """
    t = _tenant(ctx, tenant)
    data = await _api(ctx, f"/tenant/{_safepath(t)}/pipelines")
    result = [
        {"name": p["name"], "triggers": [tr["driver"] for tr in p.get("triggers", [])]}
        for p in data
    ]
    return json.dumps({"pipelines": result, "count": len(result)})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()

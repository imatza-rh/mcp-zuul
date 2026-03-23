"""Write operations (disabled by default, enable with ZUUL_READ_ONLY=false)."""

import json
from typing import Any

from mcp.server.fastmcp import Context

from ..errors import handle_errors
from ..helpers import api_delete, api_post, clean, error, safepath
from ..helpers import tenant as _tenant
from ..server import mcp
from ._common import _DESTRUCTIVE, _WRITE


@mcp.tool(title="Enqueue Change", annotations=_WRITE)
@handle_errors
async def enqueue(
    ctx: Context,
    project: str,
    pipeline: str,
    change: str = "",
    ref: str = "",
    tenant: str = "",
) -> str:
    """Enqueue a change or ref into a pipeline for testing.

    Requires ZUUL_READ_ONLY=false and a valid auth token or Kerberos ticket.
    Provide either change (e.g. "12345,1") or ref (e.g. "refs/heads/main").

    Args:
        project: Project name (e.g. "org/repo")
        pipeline: Pipeline to enqueue into (e.g. "check", "gate")
        change: Change to enqueue (e.g. "12345,1" for Gerrit)
        ref: Git ref to enqueue (for ref-based pipelines)
        tenant: Tenant name (uses default if empty)
    """
    if not change and not ref:
        return error("Either change or ref is required")
    t = _tenant(ctx, tenant)
    body: dict[str, Any] = {"pipeline": pipeline}
    if change:
        body["change"] = change
    if ref:
        body["ref"] = ref
    path = f"/tenant/{safepath(t)}/project/{safepath(project)}/enqueue"
    result = await api_post(ctx, path, body)
    return json.dumps(
        clean({"status": "enqueued", "project": project, "pipeline": pipeline, **result})
    )


@mcp.tool(title="Dequeue Change", annotations=_DESTRUCTIVE)
@handle_errors
async def dequeue(
    ctx: Context,
    project: str,
    pipeline: str,
    change: str = "",
    ref: str = "",
    tenant: str = "",
) -> str:
    """Remove a change or ref from a pipeline.

    Requires ZUUL_READ_ONLY=false and a valid auth token or Kerberos ticket.

    Args:
        project: Project name (e.g. "org/repo")
        pipeline: Pipeline to dequeue from
        change: Change to dequeue (e.g. "12345,1")
        ref: Git ref to dequeue
        tenant: Tenant name (uses default if empty)
    """
    if not change and not ref:
        return error("Either change or ref is required")
    t = _tenant(ctx, tenant)
    body: dict[str, Any] = {"pipeline": pipeline}
    if change:
        body["change"] = change
    if ref:
        body["ref"] = ref
    path = f"/tenant/{safepath(t)}/project/{safepath(project)}/dequeue"
    result = await api_post(ctx, path, body)
    return json.dumps(
        clean({"status": "dequeued", "project": project, "pipeline": pipeline, **result})
    )


@mcp.tool(title="Create Autohold", annotations=_WRITE)
@handle_errors
async def autohold_create(
    ctx: Context,
    project: str,
    job: str,
    tenant: str = "",
    reason: str = "",
    count: int = 1,
    node_hold_expiration: int = 86400,
    change: str = "",
    ref: str = "",
) -> str:
    """Create an autohold request — hold nodes after a job failure for debugging.

    Requires ZUUL_READ_ONLY=false and a valid auth token or Kerberos ticket.

    Args:
        project: Project name (e.g. "org/repo")
        job: Job name to hold nodes for
        tenant: Tenant name (uses default if empty)
        reason: Why the hold is needed
        count: Number of failed builds to hold (default 1)
        node_hold_expiration: Seconds to hold nodes (default 86400 = 24h)
        change: Specific change to match (optional)
        ref: Specific ref to match (optional)
    """
    t = _tenant(ctx, tenant)
    body: dict[str, Any] = {
        "job": job,
        "count": count,
        "node_hold_expiration": node_hold_expiration,
    }
    if reason:
        body["reason"] = reason
    if change:
        body["change"] = change
    if ref:
        body["ref_filter"] = ref
    path = f"/tenant/{safepath(t)}/project/{safepath(project)}/autohold"
    result = await api_post(ctx, path, body)
    return json.dumps(clean({"status": "created", "project": project, "job": job, **result}))


@mcp.tool(title="Delete Autohold", annotations=_DESTRUCTIVE)
@handle_errors
async def autohold_delete(
    ctx: Context,
    autohold_id: str,
    tenant: str = "",
) -> str:
    """Delete an autohold request.

    Requires ZUUL_READ_ONLY=false and a valid auth token or Kerberos ticket.

    Args:
        autohold_id: Autohold request ID (from list_autoholds)
        tenant: Tenant name (uses default if empty)
    """
    t = _tenant(ctx, tenant)
    path = f"/tenant/{safepath(t)}/autohold/{safepath(autohold_id)}"
    await api_delete(ctx, path)
    return json.dumps({"status": "deleted", "autohold_id": autohold_id})

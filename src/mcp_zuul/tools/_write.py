"""Write operations (disabled by default, enable with ZUUL_READ_ONLY=false)."""

import json
from typing import Any

from mcp.server.fastmcp import Context

from ..errors import handle_errors
from ..helpers import api, api_delete, api_post, clean, error, safepath
from ..helpers import tenant as _tenant
from ..server import mcp
from ._common import _DESTRUCTIVE, _WRITE, _resolve


@mcp.tool(title="Enqueue", annotations=_WRITE)
@handle_errors
async def enqueue(
    ctx: Context,
    project: str,
    pipeline: str,
    change: str = "",
    ref: str = "",
    oldrev: str = "",
    newrev: str = "",
    tenant: str = "",
) -> str:
    """Enqueue a change or ref into a pipeline for testing.

    Requires ZUUL_READ_ONLY=false and a valid auth token or Kerberos ticket.
    Provide either change (e.g. "12345,1") or ref (e.g. "refs/heads/main").
    For ref-based enqueue (periodic pipelines), oldrev and newrev are also sent
    (empty strings to re-trigger).

    Args:
        project: Project name (e.g. "org/repo")
        pipeline: Pipeline to enqueue into (e.g. "check", "gate")
        change: Change to enqueue (e.g. "12345,1" for Gerrit)
        ref: Git ref to enqueue (e.g. "refs/heads/main" for periodic pipelines)
        oldrev: Old revision for ref-based enqueue (empty string for re-trigger)
        newrev: New revision for ref-based enqueue (empty string for re-trigger)
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
        body["oldrev"] = oldrev
        body["newrev"] = newrev
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


@mcp.tool(title="Re-enqueue Buildset", annotations=_WRITE)
@handle_errors
async def reenqueue_buildset(
    ctx: Context,
    uuid: str = "",
    tenant: str = "",
    url: str = "",
) -> str:
    """Re-enqueue a buildset — reads project/pipeline/ref from a previous buildset and enqueues it again.

    Convenience wrapper: looks up the buildset, extracts project/pipeline/ref, and re-enqueues it.
    Useful for re-triggering periodic pipeline runs without manually looking up parameters.

    Requires ZUUL_READ_ONLY=false and a valid auth token or Kerberos ticket.

    Args:
        uuid: Buildset UUID
        tenant: Tenant name (uses default if empty)
        url: Zuul buildset URL (alternative to uuid + tenant)
    """
    bs_uuid, t = _resolve(ctx, uuid, tenant, url, "buildset")
    data = await api(ctx, f"/tenant/{safepath(t)}/buildset/{safepath(bs_uuid)}")

    pipeline = data.get("pipeline")
    if not pipeline:
        return error(f"Buildset {bs_uuid} has no pipeline")

    refs = data.get("refs") or []
    if not refs:
        return error(f"Buildset {bs_uuid} has no refs")

    first_ref = refs[0]
    project = first_ref.get("project")
    ref = first_ref.get("ref")
    if not project:
        return error(f"Buildset {bs_uuid} ref has no project")
    if not ref:
        return error(
            f"Buildset {bs_uuid} ref has no ref (change-based buildsets cannot be re-enqueued as ref)"
        )

    body: dict[str, Any] = {
        "pipeline": pipeline,
        "ref": ref,
        "oldrev": "",
        "newrev": "",
    }
    path = f"/tenant/{safepath(t)}/project/{safepath(project)}/enqueue"
    result = await api_post(ctx, path, body)
    return json.dumps(
        clean(
            {
                "status": "enqueued",
                "project": project,
                "pipeline": pipeline,
                "ref": ref,
                "from_buildset": bs_uuid,
                **result,
            }
        )
    )

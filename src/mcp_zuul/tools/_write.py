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

    Requires ZUUL_READ_ONLY=false. Provide either change or ref.

    Args:
        project: Project name (e.g. "org/repo")
        pipeline: Pipeline (e.g. "check", "gate")
        change: Change to enqueue (e.g. "12345,1")
        ref: Git ref to enqueue (e.g. "refs/heads/main")
        oldrev: Old revision for ref-based enqueue
        newrev: New revision for ref-based enqueue
        tenant: Tenant (default from env)
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


@mcp.tool(title="Promote Changes", annotations=_WRITE)
@handle_errors
async def promote(
    ctx: Context,
    pipeline: str,
    changes: list[str],
    tenant: str = "",
) -> str:
    """Promote changes to the top of a pipeline queue.

    Requires ZUUL_READ_ONLY=false.

    Args:
        pipeline: Pipeline name (e.g. "gate")
        changes: Changes to promote (e.g. ["12345,1", "12346,2"])
        tenant: Tenant (default from env)
    """
    if not changes:
        return error("At least one change is required")
    t = _tenant(ctx, tenant)
    body: dict[str, Any] = {"pipeline": pipeline, "changes": changes}
    path = f"/tenant/{safepath(t)}/promote"
    result = await api_post(ctx, path, body)
    return json.dumps(
        clean({"status": "promoted", "pipeline": pipeline, "changes": changes, **result})
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

    Requires ZUUL_READ_ONLY=false.

    Args:
        project: Project name (e.g. "org/repo")
        pipeline: Pipeline to dequeue from
        change: Change to dequeue (e.g. "12345,1")
        ref: Git ref to dequeue
        tenant: Tenant (default from env)
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

    Requires ZUUL_READ_ONLY=false.

    Args:
        project: Project name (e.g. "org/repo")
        job: Job name to hold nodes for
        tenant: Tenant (default from env)
        reason: Why the hold is needed
        count: Failed builds to hold (default 1)
        node_hold_expiration: Seconds to hold nodes (default 86400 = 24h)
        change: Change filter (optional)
        ref: Ref filter (optional)
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

    Requires ZUUL_READ_ONLY=false.

    Args:
        autohold_id: Autohold request ID (from list_autoholds)
        tenant: Tenant (default from env)
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
    """Re-enqueue a buildset — re-triggers a previous buildset's pipeline run.

    Looks up the buildset, extracts project/pipeline/ref, and re-enqueues.
    Requires ZUUL_READ_ONLY=false.

    Args:
        uuid: Buildset UUID
        tenant: Tenant (default from env)
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

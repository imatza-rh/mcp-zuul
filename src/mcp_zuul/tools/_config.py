"""Configuration, infrastructure, and project tools."""

import json
from typing import Any

from mcp.server.fastmcp import Context

from ..errors import handle_errors
from ..formatters import fmt_job_variants, fmt_project
from ..helpers import api, clean, safepath
from ..helpers import tenant as _tenant
from ..server import mcp
from ._common import _READ_ONLY


@mcp.tool(title="List Jobs", annotations=_READ_ONLY)
@handle_errors
async def list_jobs(
    ctx: Context,
    tenant: str = "",
    filter: str = "",
    limit: int = 200,
) -> str:
    """List all jobs in a tenant. Optionally filter by name substring.

    Args:
        tenant: Tenant name (uses default if empty)
        filter: Case-insensitive substring to filter job names
        limit: Max results to return (default 200, 0 for unlimited)
    """
    t = _tenant(ctx, tenant)
    data = await api(ctx, f"/tenant/{safepath(t)}/jobs")
    if filter:
        f_lower = filter.lower()
        data = [j for j in data if f_lower in j.get("name", "").lower()]
    total = len(data)
    if limit > 0:
        data = data[:limit]
    result = [
        clean(
            {
                "name": j.get("name", ""),
                "description": (j.get("description") or "")[:100] or None,
                "variants": len(j.get("variants", [])),
            }
        )
        for j in data
    ]
    out: dict[str, Any] = {"jobs": result, "count": len(result)}
    if total > len(result):
        out["total"] = total
        out["truncated"] = True
    return json.dumps(out)


@mcp.tool(title="Job Configuration", annotations=_READ_ONLY)
@handle_errors
async def get_job(
    ctx: Context,
    name: str,
    tenant: str = "",
) -> str:
    """Get job configuration — parent, nodeset, timeout, branches, and all variants.

    Args:
        name: Job name
        tenant: Tenant name (uses default if empty)
    """
    t = _tenant(ctx, tenant)
    data = await api(ctx, f"/tenant/{safepath(t)}/job/{safepath(name)}")
    return json.dumps({"name": name, "variants": fmt_job_variants(data)})


@mcp.tool(title="Project Configuration", annotations=_READ_ONLY)
@handle_errors
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
    data = await api(ctx, f"/tenant/{safepath(t)}/project/{safepath(name)}")
    return json.dumps(fmt_project(data, name=name))


@mcp.tool(title="List Pipelines", annotations=_READ_ONLY)
@handle_errors
async def list_pipelines(
    ctx: Context,
    tenant: str = "",
) -> str:
    """List all pipelines with their trigger types.

    Args:
        tenant: Tenant name (uses default if empty)
    """
    t = _tenant(ctx, tenant)
    data = await api(ctx, f"/tenant/{safepath(t)}/pipelines")
    result = [
        {
            "name": p.get("name", ""),
            "triggers": [tr.get("driver", "") for tr in p.get("triggers", [])],
        }
        for p in data
    ]
    return json.dumps({"pipelines": result, "count": len(result)})


@mcp.tool(title="Configuration Errors", annotations=_READ_ONLY)
@handle_errors
async def get_config_errors(
    ctx: Context,
    tenant: str = "",
    project: str = "",
) -> str:
    """Get Zuul configuration errors — why jobs aren't running, broken configs, missing refs.

    This is the first tool to check when a job isn't being triggered or a project
    has unexpected behavior. Returns syntax errors, missing references, and repo
    access issues for the tenant or a specific project.

    Args:
        tenant: Tenant name (uses default if empty)
        project: Filter to a specific project name (optional)
    """
    t = _tenant(ctx, tenant)
    params: dict[str, Any] = {}
    if project:
        params["project"] = project
    data = await api(ctx, f"/tenant/{safepath(t)}/config-errors", params or None)
    errors = []
    for e in data:
        sc = e.get("source_context") or {}
        errors.append(
            clean(
                {
                    "project": sc.get("project"),
                    "branch": sc.get("branch"),
                    "path": sc.get("path"),
                    "severity": e.get("severity", "error"),
                    "short_error": e.get("short_error"),
                    "error": (e.get("error") or "")[:500] or None,
                    "name": e.get("name"),
                }
            )
        )
    return json.dumps({"errors": errors, "count": len(errors)})


@mcp.tool(title="List Projects", annotations=_READ_ONLY)
@handle_errors
async def list_projects(
    ctx: Context,
    tenant: str = "",
    filter: str = "",
    limit: int = 200,
) -> str:
    """List all projects in a tenant. Optionally filter by name substring.

    Args:
        tenant: Tenant name (uses default if empty)
        filter: Case-insensitive substring to filter project names
        limit: Max results to return (default 200, 0 for unlimited)
    """
    t = _tenant(ctx, tenant)
    data = await api(ctx, f"/tenant/{safepath(t)}/projects")
    if filter:
        f_lower = filter.lower()
        data = [p for p in data if f_lower in p.get("name", "").lower()]
    total = len(data)
    if limit > 0:
        data = data[:limit]
    result = [
        clean(
            {
                "name": p.get("name", ""),
                "connection": p.get("connection_name"),
                "type": p.get("type"),
                "canonical_name": p.get("canonical_name"),
            }
        )
        for p in data
    ]
    out: dict[str, Any] = {"projects": result, "count": len(result)}
    if total > len(result):
        out["total"] = total
        out["truncated"] = True
    return json.dumps(out)


@mcp.tool(title="Nodepool Nodes", annotations=_READ_ONLY)
@handle_errors
async def list_nodes(
    ctx: Context,
    tenant: str = "",
    detail: bool = False,
    limit: int = 200,
) -> str:
    """List nodepool nodes — shows what's available, in-use, or being provisioned.

    Check this when jobs are stuck waiting for nodes. By default returns
    a summary grouped by label and state. Set detail=true for individual nodes.

    Args:
        tenant: Tenant name (uses default if empty)
        detail: Include individual node list (default false, summary only)
        limit: Max nodes in detail list (default 200, 0 for unlimited).
               Summary stats always cover all nodes regardless of limit.
    """
    t = _tenant(ctx, tenant)
    data = await api(ctx, f"/tenant/{safepath(t)}/nodes")

    # Summary by state
    states: dict[str, int] = {}
    # Summary by label+state
    by_label: dict[str, dict[str, int]] = {}
    for n in data:
        s = n.get("state", "unknown")
        states[s] = states.get(s, 0) + 1
        for label in n.get("type", []):
            if label not in by_label:
                by_label[label] = {}
            by_label[label][s] = by_label[label].get(s, 0) + 1

    # Pool health summary
    total_nodes = len(data)
    ready = states.get("ready", 0)
    in_use = states.get("in-use", 0)
    building = states.get("building", 0)
    if total_nodes == 0:
        health_status = "empty"
    elif ready == 0 and building > 0:
        health_status = "recovering"
    elif ready == 0:
        health_status = "exhausted"
    elif ready / total_nodes < 0.2:
        health_status = "stressed"
    else:
        health_status = "healthy"

    out: dict[str, Any] = {
        "count": total_nodes,
        "by_state": states,
        "by_label": by_label,
        "pool_health": {
            "total": total_nodes,
            "ready": ready,
            "in_use": in_use,
            "building": building,
            "status": health_status,
        },
    }
    if detail:
        detail_data = data if limit <= 0 else data[:limit]
        out["nodes"] = [
            clean(
                {
                    "id": n.get("id"),
                    "label": n.get("type", []),
                    "state": n.get("state"),
                    "provider": n.get("provider"),
                    "connection_type": n.get("connection_type"),
                    "external_id": n.get("external_id"),
                    "comment": n.get("comment"),
                }
            )
            for n in detail_data
        ]
        if limit > 0 and total_nodes > limit:
            out["detail_truncated"] = True
    return json.dumps(out)


@mcp.tool(title="Nodepool Labels", annotations=_READ_ONLY)
@handle_errors
async def list_labels(
    ctx: Context,
    tenant: str = "",
) -> str:
    """List available nodepool labels (node types that jobs can request).

    Args:
        tenant: Tenant name (uses default if empty)
    """
    t = _tenant(ctx, tenant)
    data = await api(ctx, f"/tenant/{safepath(t)}/labels")
    names = sorted(item.get("name", "") for item in data)
    return json.dumps({"labels": names, "count": len(names)})


@mcp.tool(title="Semaphores", annotations=_READ_ONLY)
@handle_errors
async def list_semaphores(
    ctx: Context,
    tenant: str = "",
) -> str:
    """List semaphores — resource locks that limit concurrent job execution.

    Check this when jobs are waiting unexpectedly. A semaphore at max
    holders means jobs are queued waiting for the lock to be released.

    Args:
        tenant: Tenant name (uses default if empty)
    """
    t = _tenant(ctx, tenant)
    data = await api(ctx, f"/tenant/{safepath(t)}/semaphores")
    result = [
        clean(
            {
                "name": s.get("name"),
                "max": s.get("max"),
                "global": s.get("global") or None,
                "holders_count": s.get("holders", {}).get("count", 0),
                "holders": s.get("holders", {}).get("this_tenant") or None,
                "other_tenants": s.get("holders", {}).get("other_tenants") or None,
            }
        )
        for s in data
    ]
    return json.dumps({"semaphores": result, "count": len(result)})


@mcp.tool(title="Autohold Requests", annotations=_READ_ONLY)
@handle_errors
async def list_autoholds(
    ctx: Context,
    tenant: str = "",
) -> str:
    """List autohold requests — nodes held after failure for debugging.

    Shows active autohold requests: which project/job/change triggered
    them, how many nodes are held, and expiration.

    Args:
        tenant: Tenant name (uses default if empty)
    """
    t = _tenant(ctx, tenant)
    data = await api(ctx, f"/tenant/{safepath(t)}/autohold")
    result = [
        clean(
            {
                "id": a.get("id"),
                "project": a.get("project"),
                "job": a.get("job"),
                "ref_filter": a.get("ref_filter"),
                "reason": (a.get("reason") or "")[:200] or None,
                "count": a.get("count"),
                "current_count": a.get("current_count"),
                "max_count": a.get("max_count"),
                "node_expiration": a.get("node_expiration"),
                "expired": a.get("expired"),
            }
        )
        for a in data
    ]
    return json.dumps({"autoholds": result, "count": len(result)})


@mcp.tool(title="Resolved Job Graph", annotations=_READ_ONLY)
@handle_errors
async def get_freeze_jobs(
    ctx: Context,
    pipeline: str,
    project: str,
    branch: str = "main",
    tenant: str = "",
) -> str:
    """Get the resolved job graph for a pipeline/project/branch.

    Shows exactly which jobs will run with all inheritance resolved,
    including dependencies between jobs. Use this to understand job
    ordering and why a job is (or isn't) in a pipeline.

    Args:
        pipeline: Pipeline name (e.g. "check", "gate")
        project: Project name (e.g. "openstack-k8s-operators/openstack-operator")
        branch: Branch name (default "main")
        tenant: Tenant name (uses default if empty)
    """
    t = _tenant(ctx, tenant)
    path = (
        f"/tenant/{safepath(t)}/pipeline/{safepath(pipeline)}"
        f"/project/{safepath(project)}/branch/{safepath(branch)}/freeze-jobs"
    )
    data = await api(ctx, path)
    jobs = [
        clean(
            {
                "name": j.get("name"),
                "dependencies": j.get("dependencies") or None,
            }
        )
        for j in data
    ]
    return json.dumps(
        {
            "pipeline": pipeline,
            "project": project,
            "branch": branch,
            "jobs": jobs,
            "count": len(jobs),
        }
    )


@mcp.tool(title="Source Connections", annotations=_READ_ONLY)
@handle_errors
async def get_connections(ctx: Context) -> str:
    """List configured source connections — Gerrit, GitHub, GitLab instances.

    Shows what code review systems this Zuul instance talks to,
    with connection type, hostname, and base URL.
    """
    data = await api(ctx, "/connections")
    result = [
        clean(
            {
                "name": c.get("name"),
                "driver": c.get("driver"),
                "baseurl": c.get("baseurl"),
                "canonical_hostname": c.get("canonical_hostname"),
                "server": c.get("server"),
            }
        )
        for c in data
    ]
    return json.dumps({"connections": result, "count": len(result)})


@mcp.tool(title="System Components", annotations=_READ_ONLY)
@handle_errors
async def get_components(ctx: Context) -> str:
    """Show Zuul system components — schedulers, executors, mergers, web servers.

    Check this to see if Zuul is healthy. Shows component state
    (running/paused), version, and hostname.
    """
    data = await api(ctx, "/components")
    result = {}
    for kind, instances in data.items():
        result[kind] = [
            clean(
                {
                    "hostname": c.get("hostname"),
                    "state": c.get("state"),
                    "version": c.get("version"),
                }
            )
            for c in instances
        ]
    return json.dumps(result)


@mcp.tool(title="Resolved Job Configuration", annotations=_READ_ONLY)
@handle_errors
async def get_freeze_job(
    ctx: Context,
    pipeline: str,
    project: str,
    job_name: str,
    branch: str = "main",
    tenant: str = "",
) -> str:
    """Get the fully-resolved configuration for a specific job after inheritance.

    Shows the final merged nodeset, timeout, playbooks, and variables
    after all parent job inheritance is applied. Use this to understand
    exactly what a job will do — resolves "what nodeset will it use?"
    and "which playbooks run?" questions.

    Args:
        pipeline: Pipeline name (e.g. "check", "gate")
        project: Project name (e.g. "openstack-k8s-operators/openstack-operator")
        job_name: Job name to resolve
        branch: Branch name (default "main")
        tenant: Tenant name (uses default if empty)
    """
    t = _tenant(ctx, tenant)
    path = (
        f"/tenant/{safepath(t)}/pipeline/{safepath(pipeline)}"
        f"/project/{safepath(project)}/branch/{safepath(branch)}"
        f"/freeze-job/{safepath(job_name)}"
    )
    data = await api(ctx, path)

    nodeset = data.get("nodeset") or {}
    nodes = nodeset.get("nodes", [])
    playbooks = [
        clean({"project": pb.get("project"), "path": pb.get("path"), "trusted": pb.get("trusted")})
        for pb in data.get("playbooks", [])
    ]
    pre_playbooks = [
        clean({"project": pb.get("project"), "path": pb.get("path")})
        for pb in data.get("pre_playbooks", [])
    ]
    post_playbooks = [
        clean({"project": pb.get("project"), "path": pb.get("path")})
        for pb in data.get("post_playbooks", [])
    ]

    return json.dumps(
        clean(
            {
                "job": data.get("job"),
                "timeout": data.get("timeout"),
                "post_timeout": data.get("post_timeout"),
                "nodeset": clean(
                    {
                        "name": nodeset.get("name"),
                        "nodes": [{"name": n.get("name"), "label": n.get("label")} for n in nodes]
                        or None,
                    }
                )
                if nodeset
                else None,
                "playbooks": playbooks or None,
                "pre_playbooks": pre_playbooks or None,
                "post_playbooks": post_playbooks or None,
                "vars": data.get("vars") or None,
                "extra_vars": data.get("extra_vars") or None,
                "host_vars": data.get("host_vars") or None,
                "group_vars": data.get("group_vars") or None,
                "ansible_version": data.get("ansible_version"),
            }
        )
    )


@mcp.tool(title="Tenant Information", annotations=_READ_ONLY)
@handle_errors
async def get_tenant_info(
    ctx: Context,
    tenant: str = "",
) -> str:
    """Get tenant capabilities, auth config, and websocket URL.

    Shows what features are available for this tenant (job history,
    auth realms) and the tenant name.

    Args:
        tenant: Tenant name (uses default if empty)
    """
    t = _tenant(ctx, tenant)
    data = await api(ctx, f"/tenant/{safepath(t)}/info")
    info = data.get("info", data)
    caps = info.get("capabilities", {})
    return json.dumps(
        clean(
            {
                "tenant": info.get("tenant"),
                "job_history": caps.get("job_history"),
                "auth_realms": list(caps.get("auth", {}).get("realms", {}).keys()) or None,
                "read_protected": caps.get("auth", {}).get("read_protected"),
                "websocket_url": info.get("websocket_url"),
            }
        )
    )

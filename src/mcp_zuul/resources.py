"""MCP resources exposing Zuul data as browsable, attachable context."""

import json

from mcp.server.fastmcp import Context

from .formatters import fmt_build
from .helpers import api, safepath
from .helpers import tenant as _tenant
from .server import mcp


@mcp.resource("zuul://{tenant}/build/{uuid}")
async def build_resource(tenant: str, uuid: str, ctx: Context | None = None) -> str:
    """Full build details as attachable context."""
    assert ctx is not None
    t = _tenant(ctx, tenant)
    data = await api(ctx, f"/tenant/{safepath(t)}/build/{safepath(uuid)}")
    return json.dumps(fmt_build(data, brief=False), indent=2)


@mcp.resource("zuul://{tenant}/job/{name}")
async def job_resource(tenant: str, name: str, ctx: Context | None = None) -> str:
    """Job configuration with all variants."""
    assert ctx is not None
    t = _tenant(ctx, tenant)
    data = await api(ctx, f"/tenant/{safepath(t)}/job/{safepath(name)}")
    variants = []
    for v in data:
        sc = v.get("source_context") or {}
        variants.append(
            {
                "parent": v.get("parent"),
                "branches": v.get("branches", []) or None,
                "nodeset": v.get("nodeset"),
                "timeout": v.get("timeout"),
                "voting": v.get("voting", True),
                "description": (v.get("description") or "")[:500] or None,
                "source_project": sc.get("project"),
            }
        )
    return json.dumps({"name": name, "variants": variants}, indent=2)


@mcp.resource("zuul://{tenant}/project/{name}")
async def project_resource(tenant: str, name: str, ctx: Context | None = None) -> str:
    """Project configuration — pipelines and jobs."""
    assert ctx is not None
    t = _tenant(ctx, tenant)
    data = await api(ctx, f"/tenant/{safepath(t)}/project/{safepath(name)}")
    configs: dict[str, list[str]] = {}
    for cfg in data.get("configs", []):
        for pip in cfg.get("pipelines", []):
            pname = pip.get("name", "")
            jobs = []
            for j in pip.get("jobs", []):
                if isinstance(j, list):
                    jobs.append(j[0]["name"] if j else "")
                elif isinstance(j, dict):
                    jobs.append(j.get("name", ""))
            if jobs:
                configs[pname] = jobs
    return json.dumps(
        {
            "project": name,
            "canonical_name": data.get("canonical_name"),
            "connection": data.get("connection_name"),
            "type": data.get("type"),
            "pipelines": configs,
        },
        indent=2,
    )

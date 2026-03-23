"""MCP resources exposing Zuul data as browsable, attachable context."""

import json

from mcp.server.fastmcp import Context

from .formatters import fmt_build, fmt_job_variants, fmt_project
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
    return json.dumps(
        {"name": name, "variants": fmt_job_variants(data, description_limit=500)},
        indent=2,
    )


@mcp.resource("zuul://{tenant}/project/{org}/{repo}")
async def project_resource(tenant: str, org: str, repo: str, ctx: Context | None = None) -> str:
    """Project configuration - pipelines and jobs."""
    assert ctx is not None
    t = _tenant(ctx, tenant)
    name = f"{org}/{repo}"
    data = await api(ctx, f"/tenant/{safepath(t)}/project/{safepath(name)}")
    return json.dumps(fmt_project(data, name=name), indent=2)

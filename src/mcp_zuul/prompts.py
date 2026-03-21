"""Pre-built prompt templates for common Zuul CI debugging workflows."""

import json

from mcp.server.fastmcp import Context

from .formatters import fmt_build
from .helpers import api, app, clean, fetch_log_url, safepath
from .helpers import tenant as _tenant
from .server import mcp
from .tools import _MAX_JSON_LOG_BYTES


@mcp.prompt()
async def debug_build(uuid: str, tenant: str = "", ctx: Context | None = None) -> str:
    """Investigate a CI build failure — pre-loads build details and structured failures."""
    assert ctx is not None  # FastMCP always injects ctx
    t = _tenant(ctx, tenant)
    build = await api(ctx, f"/tenant/{safepath(t)}/build/{safepath(uuid)}")
    info = fmt_build(build, brief=False)

    # Try to fetch structured failures from job-output.json
    failures = []
    log_url = build.get("log_url")
    if log_url and build.get("result") not in ("SUCCESS", "SKIPPED"):
        try:
            a = app(ctx)
            json_url = log_url.rstrip("/") + "/job-output.json.gz"
            resp = await fetch_log_url(a, json_url)
            if resp.status_code == 404:
                json_url = log_url.rstrip("/") + "/job-output.json"
                resp = await fetch_log_url(a, json_url)
            if resp.status_code == 200:
                data = json.loads(resp.content[:_MAX_JSON_LOG_BYTES])
                if isinstance(data, list):
                    for pb in data:
                        if not any(s.get("failures", 0) > 0 for s in pb.get("stats", {}).values()):
                            continue
                        for play in pb.get("plays", []):
                            for task in play.get("tasks", []):
                                for host, res in task.get("hosts", {}).items():
                                    if res.get("failed"):
                                        failures.append(
                                            clean(
                                                {
                                                    "task": task.get("task", {}).get("name", ""),
                                                    "host": host,
                                                    "msg": str(res.get("msg", ""))[:500],
                                                    "rc": res.get("rc"),
                                                    "stderr": str(res.get("stderr", ""))[:300]
                                                    or None,
                                                }
                                            )
                                        )
        except Exception:
            pass

    parts = [
        "Investigate this Zuul CI build failure:\n",
        f"## Build Details\n```json\n{json.dumps(info, indent=2)}\n```\n",
    ]
    if failures:
        parts.append(f"## Failed Tasks\n```json\n{json.dumps(failures, indent=2)}\n```\n")

    t_arg = f', tenant="{tenant}"' if tenant else ""
    parts.append(
        "## Next Steps\n"
        f'1. Run `get_build_log(uuid="{uuid}"{t_arg}, mode="summary")` '
        "for error lines and log tail\n"
        f'2. Use `get_build_log(uuid="{uuid}"{t_arg}, grep="<pattern>")` '
        "to search for specific errors\n"
        "3. Classify: infrastructure (NODE_FAILURE, timeout, network) vs "
        "code bug vs flaky test vs config error\n"
        "4. Suggest concrete fix or workaround"
    )
    return "\n".join(parts)

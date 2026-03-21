"""Pre-built prompt templates for common Zuul CI debugging workflows."""

import json

from mcp.server.fastmcp import Context

from .formatters import fmt_build, fmt_buildset, fmt_status_item
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

    # Check if this job is flaky by looking at recent build history
    flaky_hint = ""
    job_name = build.get("job_name", "")
    if job_name and build.get("result") not in ("SUCCESS", "SKIPPED"):
        try:
            recent = await api(
                ctx, f"/tenant/{safepath(t)}/builds", {"job_name": job_name, "limit": 10}
            )
            if len(recent) >= 3:
                fail_count = sum(1 for b in recent if b.get("result") == "FAILURE")
                success_count = sum(1 for b in recent if b.get("result") == "SUCCESS")
                if fail_count > 0 and success_count > 0:
                    rate = round(fail_count / len(recent) * 100)
                    flaky_hint = (
                        f"\n**Flaky signal**: {fail_count}/{len(recent)} recent builds failed "
                        f"({rate}% failure rate) with mixed results — likely flaky. "
                        "Consider rechecking before deep investigation.\n"
                    )
        except Exception:
            pass

    t_arg = f', tenant="{tenant}"' if tenant else ""
    parts.append(
        "## Next Steps\n"
        + (flaky_hint if flaky_hint else "")
        + f'1. Run `get_build_log(uuid="{uuid}"{t_arg}, mode="summary")` '
        "for error lines and log tail\n"
        f'2. Use `get_build_log(uuid="{uuid}"{t_arg}, grep="<pattern>")` '
        "to search for specific errors\n"
        "3. Classify: infrastructure (NODE_FAILURE, timeout, network) vs "
        "code bug vs flaky test vs config error\n"
        "4. Suggest concrete fix or workaround"
    )
    return "\n".join(parts)


@mcp.prompt()
async def compare_builds(
    uuid1: str, uuid2: str, tenant: str = "", ctx: Context | None = None
) -> str:
    """Compare two builds side-by-side — highlights differences in result, timing, nodeset, and failures."""
    assert ctx is not None
    t = _tenant(ctx, tenant)
    b1 = await api(ctx, f"/tenant/{safepath(t)}/build/{safepath(uuid1)}")
    b2 = await api(ctx, f"/tenant/{safepath(t)}/build/{safepath(uuid2)}")
    info1 = fmt_build(b1, brief=False)
    info2 = fmt_build(b2, brief=False)

    parts = [
        "Compare these two Zuul CI builds and identify what changed:\n",
        f"## Build A\n```json\n{json.dumps(info1, indent=2)}\n```\n",
        f"## Build B\n```json\n{json.dumps(info2, indent=2)}\n```\n",
    ]

    # Fetch failures for failed builds
    for label, build, _uuid in [("A", b1, uuid1), ("B", b2, uuid2)]:
        if build.get("result") not in ("SUCCESS", "SKIPPED", None):
            log_url = build.get("log_url")
            if log_url:
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
                            tasks = []
                            for pb in data:
                                if not any(
                                    s.get("failures", 0) > 0 for s in pb.get("stats", {}).values()
                                ):
                                    continue
                                for play in pb.get("plays", []):
                                    for task in play.get("tasks", []):
                                        for host, res in task.get("hosts", {}).items():
                                            if res.get("failed"):
                                                tasks.append(
                                                    clean(
                                                        {
                                                            "task": task.get("task", {}).get(
                                                                "name", ""
                                                            ),
                                                            "host": host,
                                                            "msg": str(res.get("msg", ""))[:300],
                                                        }
                                                    )
                                                )
                            if tasks:
                                parts.append(
                                    f"## Build {label} Failures\n"
                                    f"```json\n{json.dumps(tasks, indent=2)}\n```\n"
                                )
                except Exception:
                    pass

    parts.append(
        "## Analysis\n"
        "1. Compare results, duration, nodesets, and any error details\n"
        "2. If one passed and one failed, identify what's different in the failures\n"
        "3. Check if it's a flaky test, infrastructure change, or code regression\n"
        "4. For deeper analysis, use `get_build_log` with grep on the failed build"
    )
    return "\n".join(parts)


@mcp.prompt()
async def check_change(change: str, tenant: str = "", ctx: Context | None = None) -> str:
    """Check the current CI status of a change — live pipeline or latest results."""
    assert ctx is not None
    t = _tenant(ctx, tenant)

    # Try live status first
    data = await api(ctx, f"/tenant/{safepath(t)}/status/change/{safepath(change)}")
    if data:
        items = [fmt_status_item(item) for item in data]
        return (
            f"Live pipeline status for change {change}:\n\n"
            f"```json\n{json.dumps(items, indent=2)}\n```\n\n"
            "## Analysis\n"
            "1. Summarize the overall status (how many jobs pass/fail/pending)\n"
            "2. Highlight any failing or pre-fail jobs\n"
            "3. If jobs are queued/waiting, check `list_nodes` for node availability\n"
            "4. For failed jobs, use `get_build_failures` with the job's UUID"
        )

    # Not in pipeline — get latest buildset
    buildsets = await api(ctx, f"/tenant/{safepath(t)}/buildsets", {"change": change, "limit": 1})
    if buildsets:
        bs_uuid = buildsets[0].get("uuid")
        if bs_uuid:
            bs = await api(ctx, f"/tenant/{safepath(t)}/buildset/{safepath(bs_uuid)}")
            info = fmt_buildset(bs, brief=False)
            return (
                f"Change {change} is not currently in any pipeline.\n"
                f"Latest buildset results:\n\n"
                f"```json\n{json.dumps(info, indent=2)}\n```\n\n"
                "## Analysis\n"
                "1. Summarize the overall result and which jobs passed/failed\n"
                "2. For failed jobs, use `get_build_failures` for task-level detail\n"
                "3. If all passed, the change is ready for review/merge"
            )

    return (
        f"Change {change} is not in any pipeline and has no build history.\n\n"
        "Possible reasons:\n"
        "- The change hasn't been submitted for CI yet\n"
        "- The project may not be configured in this tenant\n"
        f'- Check `get_config_errors(project="...")` for configuration issues'
    )

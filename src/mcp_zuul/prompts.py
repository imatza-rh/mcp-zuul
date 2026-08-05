"""Pre-built prompt templates for common Zuul CI debugging workflows."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from mcp.server.mcpserver import Context  # noqa: TC002 — MCPServer resolves at runtime

from .formatters import fmt_build, fmt_buildset, fmt_status_item
from .helpers import api, safepath
from .helpers import tenant as _tenant
from .server import mcp
from .tools import _fetch_job_output


@mcp.prompt()
async def debug_build(uuid: str, tenant: str = "", ctx: Context | None = None) -> str:
    """Investigate a CI build failure - pre-loads build details and structured failures."""
    assert ctx is not None  # MCPServer always injects ctx
    t = _tenant(ctx, tenant)
    build = await api(ctx, f"/tenant/{safepath(t)}/build/{safepath(uuid)}")
    info = fmt_build(build, brief=False)

    # Fetch structured failures via shared helper
    failures: list[dict] = []
    log_url = build.get("log_url")
    if log_url and build.get("result") not in ("SUCCESS", "SKIPPED"):
        _playbooks, failures, _ok = await _fetch_job_output(ctx, log_url)

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
                        f"({rate}% failure rate) with mixed results - likely flaky. "
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
    """Compare two builds side-by-side - highlights differences in result, timing, nodeset, and failures."""
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

    # Fetch failures for failed builds via shared helper
    for label, build in [("A", b1), ("B", b2)]:
        log_url = build.get("log_url")
        if log_url and build.get("result") not in ("SUCCESS", "SKIPPED", None):
            _playbooks, tasks, _ok = await _fetch_job_output(ctx, log_url)
            if tasks:
                parts.append(
                    f"## Build {label} Failures\n```json\n{json.dumps(tasks, indent=2)}\n```\n"
                )

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
    """Check the current CI status of a change - live pipeline or latest results."""
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

    # Not in pipeline - get latest buildset
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


def _or_empty(val: Any, fallback: Any = None) -> Any:
    """Return val if it's not an Exception, else fallback (default: empty list)."""
    if isinstance(val, BaseException):
        return fallback if fallback is not None else []
    return val


@mcp.prompt()
async def tenant_health(tenant: str = "", ctx: Context | None = None) -> str:
    """Assess overall health of a Zuul tenant - components, config errors, and node pool status."""
    assert ctx is not None
    t = _tenant(ctx, tenant)

    raw = await asyncio.gather(
        api(ctx, "/components"),
        api(ctx, f"/tenant/{safepath(t)}/config-errors"),
        api(ctx, f"/tenant/{safepath(t)}/nodes"),
        return_exceptions=True,
    )
    components = _or_empty(raw[0], {})
    config_errors = _or_empty(raw[1])
    nodes = _or_empty(raw[2])

    node_counts: dict[str, int] = {}
    for n in nodes:
        state = n.get("state", "unknown").replace("-", "_")
        node_counts[state] = node_counts.get(state, 0) + 1
    node_counts["total"] = len(nodes)

    parts = [
        f'Assess the health of Zuul tenant "{t}":\n',
        f"## System Components\n```json\n{json.dumps(components, indent=2)}\n```\n",
        f"## Config Errors ({len(config_errors)})\n"
        f"```json\n{json.dumps(config_errors, indent=2)}\n```\n",
        f"## Node Pool\n```json\n{json.dumps(node_counts, indent=2)}\n```\n",
        "## Analysis\n"
        "1. Check component states - are all schedulers/executors running?\n"
        "2. Review config errors - these block jobs from running\n"
        "3. Assess node pool - low ready count may cause queue delays\n"
        f'4. For deeper investigation use `get_status(tenant="{t}")` for live pipeline state',
    ]
    return "\n".join(parts)


@mcp.prompt()
async def diagnose_queue_delay(
    tenant: str = "", pipeline: str = "", ctx: Context | None = None
) -> str:
    """Diagnose why jobs are queued or delayed - checks nodes, semaphores, and system state."""
    assert ctx is not None
    t = _tenant(ctx, tenant)

    raw = await asyncio.gather(
        api(ctx, f"/tenant/{safepath(t)}/status"),
        api(ctx, f"/tenant/{safepath(t)}/nodes"),
        api(ctx, f"/tenant/{safepath(t)}/semaphores"),
        api(ctx, "/components"),
        return_exceptions=True,
    )
    status_data = _or_empty(raw[0], {})
    nodes = _or_empty(raw[1])
    semaphores = _or_empty(raw[2])
    components = _or_empty(raw[3], {})

    all_pipelines = status_data.get("pipelines", [])
    if pipeline:
        all_pipelines = [p for p in all_pipelines if p.get("name") == pipeline]
    status_summary = []
    for p in all_pipelines:
        item_count = 0
        for queue in p.get("change_queues", []):
            for heads_group in queue.get("heads", []):
                item_count += len(heads_group)
        if item_count > 0:
            status_summary.append({"pipeline": p.get("name"), "items": item_count})

    node_counts: dict[str, int] = {}
    for n in nodes:
        state = n.get("state", "unknown").replace("-", "_")
        node_counts[state] = node_counts.get(state, 0) + 1
    node_counts["total"] = len(nodes)

    active_semaphores = []
    for s in semaphores:
        holders = s.get("holders", {})
        count = holders.get("count", 0) if isinstance(holders, dict) else 0
        if count > 0:
            active_semaphores.append(
                {
                    "name": s.get("name"),
                    "max": s.get("max"),
                    "holders": count,
                }
            )

    comp_summary: dict = {}
    for kind, instances in components.items():
        comp_summary[kind] = len(instances)
        paused = [c.get("hostname") for c in instances if c.get("state") == "paused"]
        if paused:
            comp_summary[f"{kind}_paused"] = paused

    pipeline_filter = f" (pipeline: {pipeline})" if pipeline else ""
    parts = [
        f'Diagnose queue delays in Zuul tenant "{t}"{pipeline_filter}:\n',
        f"## Pipeline Status\n```json\n{json.dumps(status_summary, indent=2)}\n```\n",
        f"## Node Pool\n```json\n{json.dumps(node_counts, indent=2)}\n```\n",
        f"## Semaphores\n```json\n{json.dumps(active_semaphores, indent=2)}\n```\n",
        f"## System Components\n```json\n{json.dumps(comp_summary, indent=2)}\n```\n",
        "## Analysis\n"
        "1. Check node pool: if ready=0 and building>0, nodes are being provisioned\n"
        "2. Check node pool: if ready=0 and building=0, pool may be exhausted\n"
        "3. Check semaphores: if max reached, jobs wait for lock release\n"
        "4. Check components: paused scheduler stops all enqueueing\n"
        "5. If queue is large but nodes available, check for job dependencies\n"
        f'6. Use `list_nodes(detail=true, tenant="{t}")` for per-node state details',
    ]
    return "\n".join(parts)

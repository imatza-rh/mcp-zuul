"""Status and analytics tools."""

import asyncio
import json
import re
from typing import Any
from urllib.parse import quote

from mcp.server.fastmcp import Context

from ..errors import handle_errors
from ..formatters import _format_duration, fmt_buildset, fmt_status_item, iter_status_items
from ..helpers import api, app, clean, error, parse_zuul_url, safepath
from ..helpers import tenant as _tenant
from ..server import mcp
from ._common import _READ_ONLY


@mcp.tool(title="List Tenants", annotations=_READ_ONLY)
@handle_errors
async def list_tenants(ctx: Context) -> str:
    """List all Zuul tenants with project and queue counts."""
    data = await api(ctx, "/tenants")
    result = [
        clean({"name": t["name"], "projects": t.get("projects", 0), "queue": t.get("queue", 0)})
        for t in data
    ]
    return json.dumps(result)


@mcp.tool(title="Pipeline Status", annotations=_READ_ONLY)
@handle_errors
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
    data = await api(ctx, f"/tenant/{safepath(t)}/status")

    all_pipelines = data.get("pipelines", [])
    if pipeline:
        all_pipelines = [p for p in all_pipelines if p.get("name") == pipeline]

    # Collect items per pipeline using the flattened iterator
    _MAX_STATUS_ITEMS = 200
    by_pipeline: dict[str, list] = {}
    total_items = 0
    for pname, item in iter_status_items(all_pipelines, project=project, active_only=active_only):
        if total_items >= _MAX_STATUS_ITEMS:
            break
        items = by_pipeline.setdefault(pname, [])
        if len(items) < 50:
            items.append(fmt_status_item(item))
            total_items += 1

    result = []
    for p in all_pipelines:
        pname = p.get("name", "")
        items = by_pipeline.get(pname, [])
        if items or not active_only:
            result.append({"pipeline": pname, "item_count": len(items), "items": items})

    if active_only:
        result = [r for r in result if r["item_count"] > 0]

    out: dict[str, Any] = {
        "zuul_version": data.get("zuul_version"),
        "pipeline_count": len(result),
        "pipelines": result,
    }
    if total_items >= _MAX_STATUS_ITEMS:
        out["capped"] = True
        out["cap_limit"] = _MAX_STATUS_ITEMS
    return json.dumps(out)


@mcp.tool(title="Change Status", annotations=_READ_ONLY)
@handle_errors
async def get_change_status(
    ctx: Context,
    change: str = "",
    tenant: str = "",
    url: str = "",
) -> str:
    """Pipeline status for a specific Gerrit change or GitHub/GitLab PR/MR.

    When the change is in the pipeline, returns live status with jobs,
    elapsed times, and buildset UUID. When not in pipeline, automatically
    fetches the latest completed buildset with all build results — no
    extra ``list_buildsets`` + ``get_buildset`` round-trips needed.

    Args:
        change: Change number (e.g. "12345"), GitHub ref ("refs/pull/123/head"),
                or GitLab ref ("refs/merge-requests/123/head")
        tenant: Tenant name (uses default if empty)
        url: Zuul change status URL (alternative to change + tenant)
    """
    if url:
        parts = parse_zuul_url(url)
        if not parts:
            raise ValueError(f"Cannot parse Zuul URL: {url}")
        url_tenant, url_kind, url_id = parts
        if url_kind != "change":
            raise ValueError(f"Expected change URL, got {url_kind}")
        change = url_id
        tenant = tenant or url_tenant
    if not change:
        raise ValueError("change or url is required")
    # Extract change number from GitHub/GitLab ref patterns so callers can
    # pass "refs/pull/123/head" or "refs/merge-requests/456/head" directly.
    ref_match = re.match(r"refs/(?:pull|merge-requests)/(\d+)/head", change)
    if ref_match:
        change = ref_match.group(1)
    t = _tenant(ctx, tenant)
    data = await api(ctx, f"/tenant/{safepath(t)}/status/change/{safepath(change)}")
    if not data:
        # Not in pipeline — fetch the latest completed buildset to save
        # the caller a list_buildsets + get_buildset round-trip.
        result: dict[str, Any] = {"change": change, "status": "not_in_pipeline"}
        try:
            buildsets = await api(
                ctx,
                f"/tenant/{safepath(t)}/buildsets",
                {"change": change, "limit": 1},
            )
            if buildsets:
                bs_uuid = buildsets[0].get("uuid")
                if bs_uuid:
                    bs_detail = await api(
                        ctx, f"/tenant/{safepath(t)}/buildset/{safepath(bs_uuid)}"
                    )
                    result["latest_buildset"] = fmt_buildset(bs_detail, brief=False)
        except Exception:
            pass  # Best-effort — don't fail the whole call
        return json.dumps(result)
    base = app(ctx).config.base_url
    formatted = [fmt_status_item(item) for item in data]
    # Enrich with status_url and tenant
    for raw, fmt in zip(data, formatted, strict=True):
        fmt["tenant"] = t
        refs = raw.get("refs", [])
        if refs:
            ref_id = refs[0].get("id", "")
            if ref_id:
                fmt["status_url"] = (
                    f"{base}/t/{safepath(t)}/status/change/{quote(ref_id, safe='/,')}"
                )
        # Make relative stream_urls absolute
        for job in fmt.get("jobs", []):
            su = job.get("stream_url", "")
            if su and not su.startswith(("http://", "https://", "ws://", "wss://")):
                job["stream_url"] = f"{base}/t/{safepath(t)}/{su}"
    return json.dumps(formatted)


@mcp.tool(title="Flaky Job Detection", annotations=_READ_ONLY)
@handle_errors
async def find_flaky_jobs(
    ctx: Context,
    job_name: str,
    tenant: str = "",
    project: str = "",
    pipeline: str = "",
    limit: int = 20,
) -> str:
    """Detect flaky jobs by analyzing recent build history for intermittent failures.

    Fetches recent builds for a job and computes pass/fail statistics.
    A job with mixed SUCCESS/FAILURE results and >20% failure rate is
    likely flaky. Returns per-result counts and the failure rate.

    Args:
        job_name: Job name to analyze
        tenant: Tenant name (uses default if empty)
        project: Filter to a specific project
        pipeline: Filter to a specific pipeline
        limit: Number of recent builds to analyze (default 20, max 100)
    """
    t = _tenant(ctx, tenant)
    limit = max(1, min(limit, 100))
    params: dict[str, Any] = {"job_name": job_name, "limit": limit}
    if project:
        params["project"] = project
    if pipeline:
        params["pipeline"] = pipeline
    data = await api(ctx, f"/tenant/{safepath(t)}/builds", params)

    results: dict[str, int] = {}
    for b in data:
        r = b.get("result") or "IN_PROGRESS"
        results[r] = results.get(r, 0) + 1

    total = len(data)
    failures = results.get("FAILURE", 0)
    infra_results = ("NODE_FAILURE", "RETRY_LIMIT", "TIMED_OUT", "DISK_FULL")
    infra_failures = sum(results.get(r, 0) for r in infra_results)
    # Completed builds = total minus non-conclusive results
    completed = (
        total
        - results.get("IN_PROGRESS", 0)
        - results.get("SKIPPED", 0)
        - results.get("ABORTED", 0)
    )
    rate = round(failures / completed * 100, 1) if completed > 0 else 0.0
    infra_rate = round(infra_failures / completed * 100, 1) if completed > 0 else 0.0
    flaky = completed >= 3 and 0 < failures < completed and rate > 20

    builds = [
        clean(
            {
                "uuid": b.get("uuid"),
                "result": b.get("result"),
                "duration": b.get("duration"),
                "start_time": b.get("start_time"),
                "pipeline": b.get("pipeline"),
                "change": b.get("ref", {}).get("change")
                if isinstance(b.get("ref"), dict)
                else None,
            }
        )
        for b in data
    ]

    return json.dumps(
        clean(
            {
                "job": job_name,
                "analyzed": total,
                "completed": completed,
                "results": results,
                "failure_rate": rate,
                "infra_failure_rate": infra_rate if infra_failures > 0 else None,
                "flaky": flaky,
                "builds": builds,
            }
        )
    )


@mcp.tool(title="Build Duration Trends", annotations=_READ_ONLY)
@handle_errors
async def get_build_times(
    ctx: Context,
    tenant: str = "",
    job_name: str = "",
    project: str = "",
    pipeline: str = "",
    branch: str = "",
    limit: int = 20,
    skip: int = 0,
) -> str:
    """Build duration trends — is a job getting slower? Compute avg/min/max from results.

    Returns build durations with timing data for trend analysis.
    Use this to detect performance regressions or timeout-prone jobs.

    Note: This endpoint returns ALL results (SUCCESS, FAILURE, etc.) and does
    not support result filtering. For filtered averages (e.g. SUCCESS-only),
    use get_job_durations instead.

    Args:
        tenant: Tenant name (uses default if empty)
        job_name: Filter by job name
        project: Filter by project name
        pipeline: Filter by pipeline name
        branch: Filter by branch name
        limit: Max results, 1-100 (default 20)
        skip: Offset for pagination
    """
    t = _tenant(ctx, tenant)
    limit = max(1, min(limit, 100))
    params: dict[str, Any] = {"limit": limit, "skip": skip}
    for key, val in [
        ("job_name", job_name),
        ("project", project),
        ("pipeline", pipeline),
        ("branch", branch),
    ]:
        if val:
            params[key] = val
    data = await api(ctx, f"/tenant/{safepath(t)}/build-times", params)

    durations = [b["duration"] for b in data if b.get("duration") is not None]
    stats = {}
    if durations:
        stats = {
            "avg": round(sum(durations) / len(durations), 1),
            "min": min(durations),
            "max": max(durations),
            "count": len(durations),
        }

    builds = [
        clean(
            {
                "uuid": b.get("uuid"),
                "job": b.get("job_name"),
                "result": b.get("result"),
                "duration": b.get("duration"),
                "start_time": b.get("start_time"),
                "project": b.get("project"),
                "pipeline": b.get("pipeline"),
            }
        )
        for b in data
    ]
    return json.dumps({"stats": stats, "builds": builds, "count": len(builds)})


@mcp.tool(title="Batch Job Duration Stats", annotations=_READ_ONLY)
@handle_errors
async def get_job_durations(
    ctx: Context,
    job_names: list[str],
    tenant: str = "",
    result: str = "SUCCESS",
    limit: int = 10,
) -> str:
    """Get avg/min/max duration for multiple jobs in a single call.

    Fetches build history for each job in parallel and computes
    duration statistics. Designed for monitoring tools that need
    avg durations for an entire pipeline chain without making N
    separate API calls.

    Args:
        job_names: List of job names to get stats for
        tenant: Tenant name (uses default if empty)
        result: Filter by result (default "SUCCESS" for clean averages)
        limit: Builds per job to analyze (default 10, max 50)
    """
    if not job_names:
        return error("job_names list is required")
    if len(job_names) > 20:
        return error("Maximum 20 job names per call")

    t = _tenant(ctx, tenant)
    limit = max(1, min(limit, 50))
    sem = asyncio.Semaphore(10)

    async def _fetch_stats(name: str) -> dict:
        async with sem:
            params: dict[str, Any] = {"job_name": name, "limit": limit}
            if result:
                params["result"] = result
            data = await api(ctx, f"/tenant/{safepath(t)}/builds", params)
            durations = [b["duration"] for b in data if b.get("duration") is not None]
            stats: dict[str, Any] = {"job": name, "builds": len(durations)}
            if len(durations) >= 3:
                avg = sum(durations) / len(durations)
                stats["avg"] = round(avg, 1)
                stats["min"] = min(durations)
                stats["max"] = max(durations)
                stats["avg_formatted"] = _format_duration(avg)
            return stats

    results = await asyncio.gather(
        *[_fetch_stats(name) for name in job_names],
        return_exceptions=True,
    )
    job_stats = []
    fetch_errors = 0
    for r in results:
        if isinstance(r, Exception):
            fetch_errors += 1
        else:
            job_stats.append(r)

    out: dict[str, Any] = {"jobs": job_stats, "count": len(job_stats)}
    if fetch_errors:
        out["fetch_errors"] = fetch_errors
    return json.dumps(out)

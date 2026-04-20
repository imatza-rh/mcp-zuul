"""Build and buildset tools."""

import asyncio
import json
import re
from typing import Any

from mcp.server.fastmcp import Context

from ..classifier import Classification, classify_failure, determine_failure_phase
from ..errors import handle_errors
from ..formatters import fmt_build, fmt_buildset
from ..helpers import api, app, clean, parse_iso_timestamp, safepath, stream_log, strip_ansi
from ..helpers import tenant as _tenant
from ..parsers import grep_log_context
from ..server import mcp
from ._common import _READ_ONLY, _fetch_job_output, _no_log_url_error, _resolve

# Matches repo-relative file paths like roles/deploy_loki/README.md
# Requires: at least one dir/ component, a filename with extension.
# Supports dotfile dirs (.github/, .zuul.d/) via optional leading dot.
# Rejects: absolute paths (/etc/...), URLs (://), path traversal (../).
_REPO_FILE_RE = re.compile(
    r"(?<![/\w])"  # not preceded by / or word char (avoids matching inside absolute paths)
    r"((?:\.?[a-zA-Z0-9_][\w.-]*/)+[\w.-]+\.\w{1,10})"
)
_FILE_PATH_NOISE = re.compile(
    r"site-packages|/home/|/root/|/tmp/|/var/|/usr/|/etc/"
    r"|\.com/|\.io/|\.org/|\.net/"  # URL-derived fragments
)


def _fallback_message(result: str, has_log_context: bool) -> str:
    """Build a context-aware fallback message when job-output.json is unavailable."""
    if has_log_context:
        return (
            "Structured job-output.json unavailable (corrupted gzip or parse error). "
            "Showing text log grep for fatal/FAILED lines."
        )
    base = "Both job-output.json and job-output.txt unavailable."
    if result == "POST_FAILURE":
        base += (
            " POST_FAILURE means the post-run playbook that uploads logs itself failed,"
            " so structured logs were never collected."
            " Try get_build_log with a different log file,"
            " or check an earlier build of the same job."
        )
    return base


def _ref_meta(build: dict) -> dict:
    """Extract ref metadata (ref_url, project, change) from a Zuul build object."""
    ref = build.get("ref")
    ref_dict = ref if isinstance(ref, dict) else {}
    return clean(
        {
            "ref_url": ref_dict.get("ref_url"),
            "project": ref_dict.get("project"),
            "change": ref_dict.get("change"),
        }
    )


def _extract_file_paths(failed_tasks: list[dict]) -> list[str] | None:
    """Extract repo-relative file paths mentioned in failure output.

    Scans msg, stdout, stderr of failed tasks plus extracted_errors
    (pre-truncation error snippets) and inner_failures (nested playbook
    failure details) for paths like ``roles/deploy_loki/README.md``.
    Returns sorted unique paths, or None if no paths found. Used to
    help consumers cross-reference failing files against the change's
    file list. Treat results as hints, not a complete inventory.
    """
    paths: set[str] = set()

    def _scan(text: str) -> None:
        for m in _REPO_FILE_RE.finditer(text):
            path = m.group(1)
            start = max(0, m.start() - 20)
            context = text[start : m.end()]
            if _FILE_PATH_NOISE.search(context):
                continue
            paths.add(path)

    for task in failed_tasks:
        for field in ("msg", "stdout", "stderr"):
            text = task.get(field)
            if text and isinstance(text, str):
                _scan(text)
        # Scan extracted_errors (pre-truncation error snippets from middle section)
        for err in task.get("extracted_errors") or []:
            if isinstance(err, str):
                _scan(err)
        # Scan inner_failures (nested playbook failure details)
        for inner in task.get("inner_failures") or []:
            if isinstance(inner, dict):
                for field in ("msg", "stderr_excerpt", "cmd", "raw"):
                    text = inner.get(field)
                    if text and isinstance(text, str):
                        _scan(text)
    return sorted(paths) or None


@mcp.tool(title="Search Builds", annotations=_READ_ONLY)
@handle_errors
async def list_builds(
    ctx: Context,
    tenant: str = "",
    project: str = "",
    pipeline: str = "",
    job_name: str = "",
    change: str = "",
    branch: str = "",
    patchset: str = "",
    ref: str = "",
    result: str = "",
    completed_after: str = "",
    completed_before: str = "",
    started_after: str = "",
    started_before: str = "",
    limit: int = 20,
    skip: int = 0,
) -> str:
    """Search builds with filters. Returns compact build summaries.

    Args:
        tenant: Tenant name (uses default if empty)
        project: Filter by project name
        pipeline: Filter by pipeline name
        job_name: Filter by job name
        change: Filter by change number
        branch: Filter by branch name
        patchset: Filter by patchset
        ref: Filter by git ref
        result: Filter by result (SUCCESS, FAILURE, TIMED_OUT, SKIPPED, etc.)
        completed_after: Filter builds completed after this time (ISO 8601, e.g. "2026-04-18T00:00:00Z")
        completed_before: Filter builds completed before this time (ISO 8601)
        started_after: Filter builds started after this time (ISO 8601)
        started_before: Filter builds started before this time (ISO 8601)
        limit: Max results, 1-100 (default 20)
        skip: Offset for pagination (default 0)
    """
    t = _tenant(ctx, tenant)
    limit = max(1, min(limit, 100))

    # Parse time filters
    completed_after_dt = parse_iso_timestamp(completed_after) if completed_after else None
    completed_before_dt = parse_iso_timestamp(completed_before) if completed_before else None
    started_after_dt = parse_iso_timestamp(started_after) if started_after else None
    started_before_dt = parse_iso_timestamp(started_before) if started_before else None

    # Check if any time filters are active
    has_time_filter = any(
        [completed_after_dt, completed_before_dt, started_after_dt, started_before_dt]
    )

    # When time filtering is active, fetch more results initially to account for filtering
    # Use 3x multiplier but cap at 300 to avoid excessive API calls
    fetch_limit = min(limit * 3, 300) if has_time_filter else limit

    params: dict[str, Any] = {"limit": fetch_limit + 1, "skip": skip}
    for key, val in [
        ("project", project),
        ("pipeline", pipeline),
        ("job_name", job_name),
        ("change", change),
        ("branch", branch),
        ("patchset", patchset),
        ("ref", ref),
        ("result", result),
    ]:
        if val:
            params[key] = val

    data = await api(ctx, f"/tenant/{safepath(t)}/builds", params)

    # Apply time-based filtering if any time parameters were provided
    if has_time_filter:
        filtered = []
        for build in data:
            # Parse build timestamps
            end_time = parse_iso_timestamp(build.get("end_time", ""))
            start_time = parse_iso_timestamp(build.get("start_time", ""))

            # Apply completed_after filter
            if completed_after_dt and end_time and end_time < completed_after_dt:
                continue

            # Apply completed_before filter
            if completed_before_dt and end_time and end_time > completed_before_dt:
                continue

            # Apply started_after filter
            if started_after_dt and start_time and start_time < started_after_dt:
                continue

            # Apply started_before filter
            if started_before_dt and start_time and start_time > started_before_dt:
                continue

            filtered.append(build)

        data = filtered

    has_more = len(data) > limit
    builds = [fmt_build(b) for b in data[:limit]]
    return json.dumps({"builds": builds, "count": len(builds), "has_more": has_more})


@mcp.tool(title="Build Details", annotations=_READ_ONLY)
@handle_errors
async def get_build(
    ctx: Context,
    uuid: str = "",
    tenant: str = "",
    url: str = "",
) -> str:
    """Get full build details — log URL, nodeset, artifacts, timing, error detail.

    Args:
        uuid: Build UUID (full or prefix from list_builds)
        tenant: Tenant name (uses default if empty)
        url: Zuul build URL (alternative to uuid + tenant, e.g.
             "https://zuul.example.com/t/tenant/build/abc123")
    """
    uuid, t = _resolve(ctx, uuid, tenant, url, "build")
    data = await api(ctx, f"/tenant/{safepath(t)}/build/{safepath(uuid)}")
    return json.dumps(fmt_build(data, brief=False))


@mcp.tool(title="Build Failure Analysis", annotations=_READ_ONLY)
@handle_errors
async def get_build_failures(
    ctx: Context,
    uuid: str = "",
    tenant: str = "",
    url: str = "",
) -> str:
    """Analyze a failed build — returns exactly which task failed, on which host, with error message and return code.

    Parses Zuul's structured job-output.json for precise failure data.
    For most use cases, prefer diagnose_build which includes all this data
    plus failure classification, log context, and timing details.

    Failure responses include ref_url/project/change and files_in_failure
    (file paths extracted from error output). Use these to check whether
    failing files are part of the change before concluding if a failure is
    change-related or a pre-existing repo issue.

    Args:
        uuid: Build UUID
        tenant: Tenant name (uses default if empty)
        url: Zuul build URL (alternative to uuid + tenant)
    """
    uuid, t = _resolve(ctx, uuid, tenant, url, "build")
    build = await api(ctx, f"/tenant/{safepath(t)}/build/{safepath(uuid)}")
    result = build.get("result", "")
    log_url = build.get("log_url")
    ref_meta = _ref_meta(build)

    # Short-circuit for non-failure builds — no need to download job-output.json
    if result in ("SUCCESS", "SKIPPED"):
        msg = (
            "Build succeeded — no failures to analyze."
            if result == "SUCCESS"
            else "Build was skipped — no failures to analyze."
        )
        return json.dumps(
            clean(
                {
                    "job": build.get("job_name", ""),
                    "result": result,
                    "log_url": log_url,
                    "duration": build.get("duration"),
                    "message": msg,
                }
            )
        )

    if not log_url:
        return _no_log_url_error(build, uuid)

    playbooks, failed_tasks, json_ok = await _fetch_job_output(ctx, log_url)

    if json_ok:
        return json.dumps(
            clean(
                {
                    "job": build.get("job_name", ""),
                    "result": build.get("result", ""),
                    "log_url": log_url,
                    "duration": build.get("duration"),
                    **ref_meta,
                    "files_in_failure": _extract_file_paths(failed_tasks),
                    "playbook_count": len(playbooks),
                    "playbooks": playbooks,
                    "failed_tasks": failed_tasks,
                }
            )
        )

    # Structured parsing failed - fall back to text log grep
    log_context: list[list[dict]] = []
    try:
        log_bytes, _truncated = await stream_log(app(ctx), log_url.rstrip("/") + "/job-output.txt")
        log_context = grep_log_context(strip_ansi(log_bytes.decode("utf-8", errors="replace")))
    except Exception:
        pass

    return json.dumps(
        clean(
            {
                "job": build.get("job_name", ""),
                "result": build.get("result", ""),
                "log_url": log_url,
                "duration": build.get("duration"),
                **ref_meta,
                "json_fallback": True,
                "failed_tasks": failed_tasks,
                "log_context": log_context or None,
                "message": _fallback_message(result, bool(log_context)),
            }
        )
    )


@mcp.tool(title="Diagnose Build Failure", annotations=_READ_ONLY)
@handle_errors
async def diagnose_build(
    ctx: Context,
    uuid: str = "",
    tenant: str = "",
    url: str = "",
) -> str:
    """One-call failure diagnosis — structured failures + relevant log context.

    Combines get_build_failures (which task failed, error message) with
    targeted log grep (surrounding context from job-output.txt). Returns
    everything needed to understand a failure in a single call.

    Includes ref_url/project/change and files_in_failure so consumers can
    check whether failing files are part of the change or pre-existing.

    Use this instead of calling get_build_failures + get_build_log separately.

    Args:
        uuid: Build UUID
        tenant: Tenant name (uses default if empty)
        url: Zuul build URL (alternative to uuid + tenant)
    """
    uuid, t = _resolve(ctx, uuid, tenant, url, "build")
    build = await api(ctx, f"/tenant/{safepath(t)}/build/{safepath(uuid)}")
    result = build.get("result", "")
    log_url = build.get("log_url")
    ref_meta = _ref_meta(build)

    if result in ("SUCCESS", "SKIPPED"):
        return json.dumps(
            clean(
                {
                    "job": build.get("job_name", ""),
                    "result": result,
                    "message": "Build succeeded — nothing to diagnose."
                    if result == "SUCCESS"
                    else "Build was skipped.",
                }
            )
        )

    if not log_url:
        return _no_log_url_error(build, uuid)

    # --- 1+2. Fetch structured failures and text log in parallel ---
    async def _fetch_log_context() -> tuple[list[list[dict]], bool]:
        try:
            log_bytes, trunc = await stream_log(app(ctx), log_url.rstrip("/") + "/job-output.txt")
            return grep_log_context(strip_ansi(log_bytes.decode("utf-8", errors="replace"))), trunc
        except Exception:
            return [], False

    (playbooks, failed_tasks, _json_ok), (log_context, log_truncated) = await asyncio.gather(
        _fetch_job_output(ctx, log_url),
        _fetch_log_context(),
    )

    # --- 3. Classify the failure and determine phase ---
    classification: Classification | None = None
    failure_phase: str | None = None
    run_phase_passed: bool | None = None

    if result not in ("SUCCESS", "SKIPPED"):
        classification = classify_failure(
            result=result,
            failed_tasks=failed_tasks,
            playbooks=playbooks,
            log_context=log_context,
        )
        failure_phase = determine_failure_phase(playbooks)
        if failure_phase:
            run_failed = any(pb.get("phase") == "run" and pb.get("failed") for pb in playbooks)
            run_phase_passed = not run_failed
        else:
            run_phase_passed = None

    # Extract node name from nodeset for SSH debugging
    nodeset = build.get("nodeset")
    node_name: str | None = None
    if isinstance(nodeset, dict):
        nodes = nodeset.get("nodes", [])
        if nodes and isinstance(nodes[0], dict):
            node_name = nodes[0].get("name")
    elif isinstance(nodeset, str) and nodeset:
        node_name = nodeset

    out: dict = {
        "job": build.get("job_name", ""),
        "result": result,
        "log_url": log_url,
        "duration": build.get("duration"),
        "start_time": build.get("start_time"),
        "end_time": build.get("end_time"),
        **ref_meta,
        "files_in_failure": _extract_file_paths(failed_tasks),
        "node_name": node_name,
        "pipeline": build.get("pipeline"),
        "playbook_count": len(playbooks),
        "playbooks": playbooks,
        "failed_tasks": failed_tasks,
        "log_context": log_context or None,
        "log_truncated": log_truncated or None,
        "failure_phase": failure_phase,
        "run_phase_passed": run_phase_passed,
    }

    if classification:
        out["classification"] = classification.category
        out["classification_reason"] = classification.reason
        out["classification_confidence"] = classification.confidence
        out["retryable"] = classification.retryable

    return json.dumps(clean(out))


@mcp.tool(title="Search Buildsets", annotations=_READ_ONLY)
@handle_errors
async def list_buildsets(
    ctx: Context,
    tenant: str = "",
    project: str = "",
    pipeline: str = "",
    change: str = "",
    branch: str = "",
    ref: str = "",
    result: str = "",
    completed_after: str = "",
    completed_before: str = "",
    started_after: str = "",
    started_before: str = "",
    limit: int = 20,
    skip: int = 0,
    include_builds: bool = False,
) -> str:
    """Search buildsets (groups of builds triggered by a single event).

    Args:
        tenant: Tenant name (uses default if empty)
        project: Filter by project
        pipeline: Filter by pipeline name
        change: Filter by change number
        branch: Filter by branch name
        ref: Filter by git ref
        result: Filter by result
        completed_after: Filter buildsets completed after this time (ISO 8601, e.g. "2026-04-18T00:00:00Z")
        completed_before: Filter buildsets completed before this time (ISO 8601)
        started_after: Filter buildsets started after this time (ISO 8601)
        started_before: Filter buildsets started before this time (ISO 8601)
        limit: Max results, 1-100 (default 20)
        skip: Offset for pagination
        include_builds: Fetch full details (builds, events) for each buildset.
                        Saves a separate get_buildset call per result, but
                        slower for large result sets. Best with limit <= 5.
    """
    t = _tenant(ctx, tenant)
    limit = max(1, min(limit, 100))

    # Parse time filters
    completed_after_dt = parse_iso_timestamp(completed_after) if completed_after else None
    completed_before_dt = parse_iso_timestamp(completed_before) if completed_before else None
    started_after_dt = parse_iso_timestamp(started_after) if started_after else None
    started_before_dt = parse_iso_timestamp(started_before) if started_before else None

    # Check if any time filters are active
    has_time_filter = any(
        [completed_after_dt, completed_before_dt, started_after_dt, started_before_dt]
    )

    # When time filtering is active, fetch more results initially
    fetch_limit = min(limit * 3, 300) if has_time_filter else limit

    params: dict[str, Any] = {"limit": fetch_limit + 1, "skip": skip}
    for key, val in [
        ("project", project),
        ("pipeline", pipeline),
        ("change", change),
        ("branch", branch),
        ("ref", ref),
        ("result", result),
    ]:
        if val:
            params[key] = val

    data = await api(ctx, f"/tenant/{safepath(t)}/buildsets", params)

    # Apply time-based filtering if any time parameters were provided
    if has_time_filter:
        filtered = []
        for buildset in data:
            # Parse buildset timestamps
            # Buildsets may have first_build_start_time and last_build_end_time
            first_start = parse_iso_timestamp(buildset.get("first_build_start_time", ""))
            last_end = parse_iso_timestamp(buildset.get("last_build_end_time", ""))

            # Apply completed_after filter
            if completed_after_dt and last_end and last_end < completed_after_dt:
                continue

            # Apply completed_before filter
            if completed_before_dt and last_end and last_end > completed_before_dt:
                continue

            # Apply started_after filter
            if started_after_dt and first_start and first_start < started_after_dt:
                continue

            # Apply started_before filter
            if started_before_dt and first_start and first_start > started_before_dt:
                continue

            filtered.append(buildset)

        data = filtered

    has_more = len(data) > limit
    trimmed = data[:limit]

    if include_builds:
        cap = min(limit, 10)  # cap detail fetches to prevent huge responses
        if len(trimmed) > cap:
            has_more = True  # more data available than returned
        trimmed = trimmed[:cap]
    if include_builds and trimmed:
        sem = asyncio.Semaphore(10)

        async def _fetch_bs(bs_uuid: str) -> Any:
            async with sem:
                return await api(ctx, f"/tenant/{safepath(t)}/buildset/{safepath(bs_uuid)}")

        details = await asyncio.gather(
            *[_fetch_bs(bs["uuid"]) for bs in trimmed if bs.get("uuid")],
            return_exceptions=True,
        )
        buildsets = []
        fetch_errors = 0
        for d in details:
            if isinstance(d, Exception):
                fetch_errors += 1
                continue
            buildsets.append(fmt_buildset(d, brief=False))  # type: ignore[arg-type]
    else:
        buildsets = [fmt_buildset(bs) for bs in trimmed]
        fetch_errors = 0

    result_dict: dict[str, Any] = {
        "buildsets": buildsets,
        "count": len(buildsets),
        "has_more": has_more,
    }
    if fetch_errors:
        result_dict["fetch_errors"] = fetch_errors
    return json.dumps(result_dict)


@mcp.tool(title="Buildset Details", annotations=_READ_ONLY)
@handle_errors
async def get_buildset(
    ctx: Context,
    uuid: str = "",
    tenant: str = "",
    url: str = "",
) -> str:
    """Get full buildset details — all builds, results, events, and timing.

    Args:
        uuid: Buildset UUID
        tenant: Tenant name (uses default if empty)
        url: Zuul buildset URL (alternative to uuid + tenant)
    """
    uuid, t = _resolve(ctx, uuid, tenant, url, "buildset")
    data = await api(ctx, f"/tenant/{safepath(t)}/buildset/{safepath(uuid)}")
    return json.dumps(fmt_buildset(data, brief=False))

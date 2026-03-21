"""Zuul MCP tool implementations — 33 tools (28 read-only + 5 write)."""

import asyncio
import json
import re
from typing import Any
from urllib.parse import quote, urlparse

from mcp.server.fastmcp import Context
from mcp.types import ToolAnnotations

from .errors import handle_errors
from .formatters import fmt_build, fmt_buildset, fmt_status_item
from .helpers import (
    api,
    api_delete,
    api_post,
    app,
    clean,
    error,
    fetch_log_url,
    parse_zuul_url,
    safepath,
    stream_log,
    strip_ansi,
)
from .helpers import tenant as _tenant
from .server import mcp

_READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)

_WRITE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)

_DESTRUCTIVE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=True,
    openWorldHint=True,
)


def _resolve(
    ctx: Context, uuid: str, tenant: str, url: str, kind: str = "build"
) -> tuple[str, str]:
    """Resolve resource ID and tenant from explicit params or Zuul URL."""
    if url:
        parts = parse_zuul_url(url)
        if not parts:
            raise ValueError(f"Cannot parse Zuul URL: {url}")
        url_tenant, url_kind, url_id = parts
        if url_kind != kind:
            raise ValueError(f"Expected {kind} URL, got {url_kind}")
        return url_id, _tenant(ctx, tenant or url_tenant)
    if not uuid:
        raise ValueError(f"{kind} identifier or url is required")
    return uuid, _tenant(ctx, tenant)


# Log fetching constants
_MAX_LOG_LINES = 200
_MAX_JSON_LOG_BYTES = 20 * 1024 * 1024  # 20 MB (JSON is larger)
_MAX_FILE_BYTES = 512 * 1024  # 512 KB for fetched log files
_ERROR_PATTERNS = re.compile(
    r"(FAILED!|UNREACHABLE|fatal:|Traceback|failed=[1-9])",
)
_ERROR_NOISE = re.compile(r"failed=0|RETRYING:")


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

    pipelines = data.get("pipelines", [])
    result = []
    for p in pipelines:
        if pipeline and p["name"] != pipeline:
            continue
        items = []
        for queue in p.get("change_queues", []):
            for heads_group in queue.get("heads", []):
                for item in heads_group:
                    if project:
                        item_projects = [r.get("project", "") for r in item.get("refs", [])]
                        if not any(project in proj for proj in item_projects):
                            continue
                    if active_only and not item.get("active", False):
                        continue
                    items.append(fmt_status_item(item))
                    if len(items) >= 50:
                        break
                if len(items) >= 50:
                    break
            if len(items) >= 50:
                break
        if items or not active_only:
            result.append(
                {
                    "pipeline": p["name"],
                    "item_count": len(items),
                    "items": items,
                }
            )

    # Only include pipelines with items when active_only
    if active_only:
        result = [r for r in result if r["item_count"] > 0]

    return json.dumps(
        {
            "zuul_version": data.get("zuul_version"),
            "pipeline_count": len(result),
            "pipelines": result,
        }
    )


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
    t = _tenant(ctx, tenant)
    data = await api(ctx, f"/tenant/{safepath(t)}/status/change/{safepath(change)}")
    if not data and change.isdigit():
        # Zuul status/change API doesn't match bare numbers to GitLab-style
        # refs. Fall back to filtering the full status by change number.
        full = await api(ctx, f"/tenant/{safepath(t)}/status")
        items = []
        for p in full.get("pipelines", []):
            for queue in p.get("change_queues", []):
                for heads_group in queue.get("heads", []):
                    for item in heads_group:
                        for r in item.get("refs", []):
                            ref_str = r.get("ref", "")
                            if f"/{change}/" in ref_str:
                                items.append(item)
                                break
        data = items
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
    # Enrich with status_url — constructed from ref id ("{change},{sha}")
    for raw, fmt in zip(data, formatted, strict=True):
        refs = raw.get("refs", [])
        if refs:
            ref_id = refs[0].get("id", "")
            if ref_id:
                fmt["status_url"] = (
                    f"{base}/t/{safepath(t)}/status/change/{quote(ref_id, safe='/,')}"
                )
    return json.dumps(formatted)


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
        limit: Max results, 1-100 (default 20)
        skip: Offset for pagination (default 0)
    """
    t = _tenant(ctx, tenant)
    limit = max(1, min(limit, 100))
    params: dict[str, Any] = {"limit": limit + 1, "skip": skip}
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
    has_more = len(data) > limit
    builds = [fmt_build(b) for b in data[:limit]]
    return json.dumps({"builds": builds, "count": len(builds), "has_more": has_more, "skip": skip})


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
    Start here when investigating build failures — much more accurate than log parsing.

    Args:
        uuid: Build UUID
        tenant: Tenant name (uses default if empty)
        url: Zuul build URL (alternative to uuid + tenant)
    """
    uuid, t = _resolve(ctx, uuid, tenant, url, "build")
    build = await api(ctx, f"/tenant/{safepath(t)}/build/{safepath(uuid)}")
    log_url = build.get("log_url")
    if not log_url:
        return error(f"No log_url for build {uuid}")

    a = app(ctx)
    json_url = log_url.rstrip("/") + "/job-output.json.gz"
    resp = await fetch_log_url(a, json_url)
    if resp.status_code == 404:
        # Fall back to uncompressed
        json_url = log_url.rstrip("/") + "/job-output.json"
        resp = await fetch_log_url(a, json_url)
    if resp.status_code == 404:
        return error("job-output.json not found")
    resp.raise_for_status()

    raw = resp.content[:_MAX_JSON_LOG_BYTES]
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        return error(f"Failed to parse job-output.json: {e}")

    if not isinstance(data, list):
        return error("Unexpected job-output.json format")

    playbooks = []
    failed_tasks = []
    for pb in data:
        phase = pb.get("phase", "")
        playbook = pb.get("playbook", "")
        stats = pb.get("stats", {})
        has_failure = any(s.get("failures", 0) > 0 for s in stats.values())

        # Only include playbooks that have failures to save tokens
        if has_failure:
            pb_summary = clean(
                {
                    "phase": phase,
                    "playbook": playbook.split("/")[-1] if "/" in playbook else playbook,
                    "playbook_full": playbook,
                    "stats": stats,
                }
            )
            playbooks.append(pb_summary)
            for play in pb.get("plays", []):
                play_name = play.get("play", {}).get("name", "")
                for task in play.get("tasks", []):
                    task_info = task.get("task", {})
                    task_name = task_info.get("name", "")
                    duration = task_info.get("duration", {})
                    for host, res in task.get("hosts", {}).items():
                        if res.get("failed"):
                            ft = clean(
                                {
                                    "play": play_name,
                                    "task": task_name,
                                    "host": host,
                                    "msg": str(res.get("msg", ""))[:1000],
                                    "rc": res.get("rc"),
                                    "stderr": str(res.get("stderr", ""))[:1000] or None,
                                    "stdout": str(res.get("stdout", ""))[:1000] or None,
                                    "duration": duration.get("end", ""),
                                    "playbook": playbook,
                                }
                            )
                            failed_tasks.append(ft)

    return json.dumps(
        clean(
            {
                "job": build.get("job_name", ""),
                "result": build.get("result", ""),
                "log_url": log_url,
                "duration": build.get("duration"),
                "total_playbooks": len(data),
                "failed_playbooks": playbooks,
                "failed_tasks": failed_tasks,
            }
        )
    )


@mcp.tool(title="Read Build Log", annotations=_READ_ONLY)
@handle_errors
async def get_build_log(
    ctx: Context,
    uuid: str = "",
    tenant: str = "",
    log_name: str = "job-output.txt",
    mode: str = "summary",
    lines: int = 0,
    start_line: int = 0,
    end_line: int = 0,
    grep: str = "",
    context: int = 0,
    url: str = "",
) -> str:
    """Read, search, and navigate build log files with grep, line ranges, and error summary.

    Args:
        uuid: Build UUID
        tenant: Tenant name (uses default if empty)
        log_name: Log file to read (default "job-output.txt"). For other files,
                  use the path relative to the build's log_url, e.g.
                  "logs/controller/ci-framework-data/logs/ci_script_008_run.log"
        mode: "summary" (default: tail + error lines) or "full" (paginated chunks)
        lines: For summary: tail line count (default 100). For full: offset start line.
        start_line: Read from this line number (1-based). If set with end_line,
                    returns exactly that range (overrides mode).
        end_line: Read up to this line number (1-based, inclusive).
        grep: Python regex pattern to filter log lines (overrides mode).
              Use | for OR: "error|failed|timeout". Do NOT use backslash-pipe.
        context: Lines of context before/after each grep match (default 0, max 10)
        url: Zuul build URL (alternative to uuid + tenant)
    """
    uuid, t = _resolve(ctx, uuid, tenant, url, "build")
    build = await api(ctx, f"/tenant/{safepath(t)}/build/{safepath(uuid)}")
    log_url = build.get("log_url")
    if not log_url:
        return error(f"No log_url for build {uuid}")

    # Sanitize log_name to prevent path traversal
    if ".." in log_name.split("/"):
        return error(f"Invalid log_name: {log_name!r}")
    txt_url = log_url.rstrip("/") + "/" + log_name.lstrip("/")

    a = app(ctx)
    raw = strip_ansi((await stream_log(a, txt_url)).decode("utf-8", errors="replace"))
    all_lines = raw.splitlines()
    total = len(all_lines)

    # Line range mode (start_line/end_line)
    if start_line > 0:
        if start_line > total:
            return error(f"start_line {start_line} exceeds total {total} lines")
        s = start_line - 1  # convert to 0-based
        e = (end_line if end_line > 0 else start_line + _MAX_LOG_LINES) - 1
        e = min(e, total - 1)
        chunk_lines = all_lines[s : e + 1]
        return json.dumps(
            {
                "total_lines": total,
                "log_url": txt_url,
                "start_line": start_line,
                "end_line": e + 1,
                "count": len(chunk_lines),
                "lines": [
                    {"n": s + i + 1, "text": line[:500]} for i, line in enumerate(chunk_lines)
                ],
            }
        )

    # Grep mode
    if grep:
        # Auto-fix common shell-grep-to-python-regex mistake: \| -> |
        if r"\|" in grep and "|" not in grep.replace(r"\|", ""):
            grep = grep.replace(r"\|", "|")
        try:
            pat = re.compile(grep, re.IGNORECASE)
        except re.error as e:
            return error(f"Invalid regex: {e}")
        try:
            matched = await asyncio.wait_for(
                asyncio.get_running_loop().run_in_executor(
                    None,
                    lambda: [(i + 1, line) for i, line in enumerate(all_lines) if pat.search(line)],
                ),
                timeout=10.0,
            )
        except TimeoutError:
            return error("Regex search timed out (pattern may be too complex)")
        ctx_n = max(0, min(context, 10))
        if ctx_n > 0 and matched:
            # Build context blocks around each match
            blocks = []
            for n, _text in matched[:50]:
                start = max(0, n - 1 - ctx_n)
                end = min(total, n + ctx_n)
                block = [
                    {
                        "n": i + 1,
                        "text": all_lines[i][:500],
                        "match": pat.search(all_lines[i]) is not None,
                    }
                    for i in range(start, end)
                ]
                blocks.append(block)
            return json.dumps(
                {
                    "total_lines": total,
                    "log_url": txt_url,
                    "grep": grep,
                    "matched": len(matched),
                    "blocks": blocks,
                }
            )
        return json.dumps(
            {
                "total_lines": total,
                "log_url": txt_url,
                "grep": grep,
                "matched": len(matched),
                "lines": [{"n": n, "text": text[:500]} for n, text in matched[:100]],
            }
        )

    # Summary mode — single pass for both errors and tail
    if mode == "summary":
        tail_n = lines or 100
        tail_start = max(0, total - tail_n)
        errors: list[tuple[int, str]] = []
        tail: list[str] = []
        for i, line in enumerate(all_lines):
            if _ERROR_PATTERNS.search(line) and not _ERROR_NOISE.search(line) and len(errors) < 30:
                errors.append((i + 1, line))
            if i >= tail_start:
                tail.append(line)
        return json.dumps(
            {
                "total_lines": total,
                "log_url": txt_url,
                "job": build.get("job_name", ""),
                "result": build.get("result", ""),
                "error_lines": [{"n": n, "text": t[:500]} for n, t in errors],
                "tail": [line[:500] for line in tail],
            }
        )

    # Full mode (paginated)
    offset = lines or 0
    chunk_lines = all_lines[offset : offset + _MAX_LOG_LINES]
    return json.dumps(
        {
            "total_lines": total,
            "log_url": txt_url,
            "offset": offset,
            "count": len(chunk_lines),
            "has_more": offset + len(chunk_lines) < total,
            "lines": [line[:500] for line in chunk_lines],
        }
    )


@mcp.tool(title="Browse Log Files", annotations=_READ_ONLY)
@handle_errors
async def browse_build_logs(
    ctx: Context,
    uuid: str = "",
    tenant: str = "",
    path: str = "",
    url: str = "",
) -> str:
    """Browse or fetch files from a build's log directory.

    Without path: lists the top-level log directory.
    With path ending in '/': lists that subdirectory.
    With path to a file: fetches and returns the file content (max 512KB).

    Args:
        uuid: Build UUID
        tenant: Tenant name (uses default if empty)
        path: Relative path within the log dir (e.g. "logs/controller/",
              "zuul-info/inventory.yaml", "logs/hypervisor/ci-framework-data/artifacts/")
        url: Zuul build URL (alternative to uuid + tenant)
    """
    uuid, t = _resolve(ctx, uuid, tenant, url, "build")
    build = await api(ctx, f"/tenant/{safepath(t)}/build/{safepath(uuid)}")
    log_url = build.get("log_url")
    if not log_url:
        return error(f"No log_url for build {uuid}")

    parsed = urlparse(log_url)
    if parsed.scheme not in ("http", "https"):
        return error(f"Invalid log URL scheme: {parsed.scheme}")

    # Prevent path traversal
    if ".." in path.split("/"):
        return error("Path traversal not allowed")

    a = app(ctx)
    target_url = log_url.rstrip("/") + "/" + path.lstrip("/")

    resp = await fetch_log_url(a, target_url)
    if resp.status_code == 404:
        return error(f"Not found: {path or '/'}")
    resp.raise_for_status()

    content_type = resp.headers.get("content-type", "")

    # Directory listing (Apache/nginx index page)
    if "text/html" in content_type and (not path or path.endswith("/")):
        entries = re.findall(r'href="([^"?][^"]*)"', resp.text)
        # Filter out parent directory and absolute links
        entries = [e for e in entries if not e.startswith("/") and not e.startswith("http")]
        return json.dumps(
            {
                "log_url": target_url,
                "path": path or "/",
                "entries": entries,
            }
        )

    # File content
    raw = resp.content[:_MAX_FILE_BYTES]
    try:
        text = raw.decode("utf-8", errors="replace")
    except Exception:
        return error(f"Cannot decode file at {path}")
    truncated = len(resp.content) > _MAX_FILE_BYTES
    return json.dumps(
        {
            "log_url": target_url,
            "path": path,
            "size": len(resp.content),
            "truncated": truncated,
            "content": text,
        }
    )


@mcp.tool(title="Log Tail", annotations=_READ_ONLY)
@handle_errors
async def tail_build_log(
    ctx: Context,
    uuid: str = "",
    tenant: str = "",
    lines: int = 50,
    log_name: str = "job-output.txt",
    url: str = "",
) -> str:
    """Get the last N lines of a build log — fastest way to see why a build failed.

    More token-efficient than get_build_log(mode="summary") when you just
    need the tail. Use this as the first step when investigating failures.

    Args:
        uuid: Build UUID
        tenant: Tenant name (uses default if empty)
        lines: Number of lines from the end (default 50, max 500)
        log_name: Log file to read (default "job-output.txt")
        url: Zuul build URL (alternative to uuid + tenant)
    """
    uuid, t = _resolve(ctx, uuid, tenant, url, "build")
    build = await api(ctx, f"/tenant/{safepath(t)}/build/{safepath(uuid)}")
    log_url = build.get("log_url")
    if not log_url:
        return error(f"No log_url for build {uuid}")
    if ".." in log_name.split("/"):
        return error(f"Invalid log_name: {log_name!r}")

    a = app(ctx)
    txt_url = log_url.rstrip("/") + "/" + log_name.lstrip("/")
    raw = strip_ansi((await stream_log(a, txt_url)).decode("utf-8", errors="replace"))
    all_lines = raw.splitlines()
    total = len(all_lines)
    n = max(1, min(lines, 500))
    tail_start = max(0, total - n)
    tail = all_lines[tail_start:]

    return json.dumps(
        {
            "total_lines": total,
            "log_url": txt_url,
            "job": build.get("job_name", ""),
            "result": build.get("result", ""),
            "tail_from": tail_start + 1,
            "count": len(tail),
            "lines": [line[:500] for line in tail],
        }
    )


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
        limit: Max results, 1-100 (default 20)
        skip: Offset for pagination
        include_builds: Fetch full details (builds, events) for each buildset.
                        Saves a separate get_buildset call per result, but
                        slower for large result sets. Best with limit <= 5.
    """
    t = _tenant(ctx, tenant)
    limit = max(1, min(limit, 100))
    params: dict[str, Any] = {"limit": limit + 1, "skip": skip}
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
    has_more = len(data) > limit
    trimmed = data[:limit]

    if include_builds and trimmed:
        details = await asyncio.gather(
            *[
                api(ctx, f"/tenant/{safepath(t)}/buildset/{safepath(bs['uuid'])}")
                for bs in trimmed
                if bs.get("uuid")
            ],
            return_exceptions=True,
        )
        buildsets = []
        for d in details:
            if isinstance(d, Exception):
                continue
            buildsets.append(fmt_buildset(d, brief=False))  # type: ignore[arg-type]
    else:
        buildsets = [fmt_buildset(bs) for bs in trimmed]

    return json.dumps(
        {
            "buildsets": buildsets,
            "count": len(buildsets),
            "has_more": has_more,
            "skip": skip,
        }
    )


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


@mcp.tool(title="List Jobs", annotations=_READ_ONLY)
@handle_errors
async def list_jobs(
    ctx: Context,
    tenant: str = "",
    filter: str = "",
) -> str:
    """List all jobs in a tenant. Optionally filter by name substring.

    Args:
        tenant: Tenant name (uses default if empty)
        filter: Case-insensitive substring to filter job names
    """
    t = _tenant(ctx, tenant)
    data = await api(ctx, f"/tenant/{safepath(t)}/jobs")
    if filter:
        f_lower = filter.lower()
        data = [j for j in data if f_lower in j.get("name", "").lower()]
    result = [
        clean(
            {
                "name": j["name"],
                "description": (j.get("description") or "")[:100] or None,
                "variants": len(j.get("variants", [])),
            }
        )
        for j in data
    ]
    return json.dumps({"jobs": result, "count": len(result)})


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
    variants = []
    for v in data:
        sc = v.get("source_context") or {}
        compact = {
            "parent": v.get("parent"),
            "branches": v.get("branches", []) or None,
            "nodeset": v.get("nodeset"),
            "timeout": v.get("timeout"),
            "voting": v.get("voting", True),
            "abstract": v.get("abstract", False) or None,
            "description": (v.get("description") or "")[:200] or None,
            "source_project": sc.get("project"),
        }
        variants.append(clean(compact))
    return json.dumps({"name": name, "variants": variants})


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
        clean(
            {
                "project": name,
                "canonical_name": data.get("canonical_name"),
                "connection": data.get("connection_name"),
                "type": data.get("type"),
                "pipelines": configs,
            }
        )
    )


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
        {"name": p["name"], "triggers": [tr["driver"] for tr in p.get("triggers", [])]}
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
) -> str:
    """List all projects in a tenant. Optionally filter by name substring.

    Args:
        tenant: Tenant name (uses default if empty)
        filter: Case-insensitive substring to filter project names
    """
    t = _tenant(ctx, tenant)
    data = await api(ctx, f"/tenant/{safepath(t)}/projects")
    if filter:
        f_lower = filter.lower()
        data = [p for p in data if f_lower in p.get("name", "").lower()]
    result = [
        clean(
            {
                "name": p["name"],
                "connection": p.get("connection_name"),
                "type": p.get("type"),
                "canonical_name": p.get("canonical_name"),
            }
        )
        for p in data
    ]
    return json.dumps({"projects": result, "count": len(result)})


@mcp.tool(title="Nodepool Nodes", annotations=_READ_ONLY)
@handle_errors
async def list_nodes(
    ctx: Context,
    tenant: str = "",
    detail: bool = False,
) -> str:
    """List nodepool nodes — shows what's available, in-use, or being provisioned.

    Check this when jobs are stuck waiting for nodes. By default returns
    a summary grouped by label and state. Set detail=true for individual nodes.

    Args:
        tenant: Tenant name (uses default if empty)
        detail: Include individual node list (default false, summary only)
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

    out: dict[str, Any] = {
        "count": len(data),
        "by_state": states,
        "by_label": by_label,
    }
    if detail:
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
            for n in data
        ]
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
    rate = round(failures / total * 100, 1) if total > 0 else 0.0
    flaky = total >= 3 and 0 < failures < total and rate > 20

    builds = [
        clean(
            {
                "uuid": b.get("uuid"),
                "result": b.get("result"),
                "duration": b.get("duration"),
                "start_time": b.get("start_time"),
                "pipeline": b.get("pipeline"),
                "ref": b.get("ref"),
            }
        )
        for b in data
    ]

    return json.dumps(
        clean(
            {
                "job": job_name,
                "analyzed": total,
                "results": results,
                "failure_rate": rate,
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


# -- Write operations (disabled by default, enable with ZUUL_READ_ONLY=false) --


@mcp.tool(title="Enqueue Change", annotations=_WRITE)
@handle_errors
async def enqueue(
    ctx: Context,
    project: str,
    pipeline: str,
    change: str = "",
    ref: str = "",
    tenant: str = "",
) -> str:
    """Enqueue a change or ref into a pipeline for testing.

    Requires ZUUL_READ_ONLY=false and a valid auth token or Kerberos ticket.
    Provide either change (e.g. "12345,1") or ref (e.g. "refs/heads/main").

    Args:
        project: Project name (e.g. "org/repo")
        pipeline: Pipeline to enqueue into (e.g. "check", "gate")
        change: Change to enqueue (e.g. "12345,1" for Gerrit)
        ref: Git ref to enqueue (for ref-based pipelines)
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


# -- LogJuicer integration (optional, requires LOGJUICER_URL) --


@mcp.tool(title="Log Anomaly Detection", annotations=_READ_ONLY)
@handle_errors
async def get_build_anomalies(
    ctx: Context,
    uuid: str = "",
    tenant: str = "",
    url: str = "",
) -> str:
    """Detect anomalous log lines using LogJuicer ML-based analysis.

    Compares failed build logs against successful baselines to find
    lines that are unusual. Requires LOGJUICER_URL to be configured.
    Accepts a build UUID or Zuul build URL.

    Args:
        uuid: Build UUID
        tenant: Tenant name (uses default if empty)
        url: Zuul build URL (alternative to uuid + tenant)
    """
    a = app(ctx)
    if not a.config.logjuicer_url:
        return error("LogJuicer not configured (set LOGJUICER_URL)")

    uuid, t = _resolve(ctx, uuid, tenant, url, "build")
    build = await api(ctx, f"/tenant/{safepath(t)}/build/{safepath(uuid)}")
    log_url = build.get("log_url")
    if not log_url:
        return error(f"No log_url for build {uuid}")

    # Build the Zuul build URL for LogJuicer
    build_url = f"{a.config.base_url}/t/{quote(t, safe='/')}/build/{quote(uuid)}"

    # Request a LogJuicer report — use log_client (no auth headers)
    # to avoid leaking Zuul tokens to the LogJuicer host
    report_url = f"{a.config.logjuicer_url}/api/report/new"
    resp = await a.log_client.put(
        report_url,
        params={"target": build_url, "errors": "true"},
        follow_redirects=True,
    )
    if resp.status_code != 200:
        return error(f"LogJuicer report creation failed: {resp.status_code}")

    report_data = resp.json()
    report_id = report_data.get("id") or report_data.get("report_id")
    if not report_id:
        return error("LogJuicer returned no report ID")

    # Fetch the report JSON
    report_resp = await a.log_client.get(
        f"{a.config.logjuicer_url}/api/report/{report_id}/json",
        follow_redirects=True,
    )
    if report_resp.status_code != 200:
        return error(f"LogJuicer report fetch failed: {report_resp.status_code}")

    report = report_resp.json()
    anomalies = []
    for source in report if isinstance(report, list) else [report]:
        for anomaly in source.get("anomalies", []):
            anomalies.append(
                clean(
                    {
                        "line": anomaly.get("line"),
                        "pos": anomaly.get("pos"),
                        "before": anomaly.get("before"),
                        "after": anomaly.get("after"),
                    }
                )
            )

    return json.dumps(
        {
            "job": build.get("job_name", ""),
            "result": build.get("result", ""),
            "report_id": report_id,
            "anomaly_count": len(anomalies),
            "anomalies": anomalies[:50],
        }
    )

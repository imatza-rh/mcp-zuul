"""Log reading and browsing tools."""

import asyncio
import json
import re
from typing import Any
from urllib.parse import urlparse

from mcp.server.fastmcp import Context

from ..errors import handle_errors
from ..helpers import api, app, error, fetch_log_url, safepath, stream_log, strip_ansi
from ..server import mcp
from ._common import (
    _ERROR_NOISE,
    _ERROR_PATTERNS,
    _MAX_FILE_BYTES,
    _MAX_LOG_LINES,
    _READ_ONLY,
    _no_log_url_error,
    _resolve,
)

_RUN_END_MARKER = re.compile(r"\| RUN END RESULT_")


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
        return _no_log_url_error(build, uuid)

    # Sanitize log_name to prevent path traversal
    if ".." in log_name.split("/"):
        return error(f"Invalid log_name: {log_name!r}")
    txt_url = log_url.rstrip("/") + "/" + log_name.lstrip("/")

    a = app(ctx)
    log_bytes, truncated = await stream_log(a, txt_url)
    raw = strip_ansi(log_bytes.decode("utf-8", errors="replace"))
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
        result_dict: dict[str, Any] = {
            "total_lines": total,
            "log_url": txt_url,
            "start_line": start_line,
            "end_line": e + 1,
            "count": len(chunk_lines),
            "lines": [{"n": s + i + 1, "text": line[:500]} for i, line in enumerate(chunk_lines)],
        }
        if truncated:
            result_dict["truncated"] = True
        return json.dumps(result_dict)

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
            # Truncate lines before matching to bound regex backtracking time.
            # Without this, pathological patterns (e.g. "(a+)+b") on long lines
            # could keep the thread pool worker busy indefinitely — the
            # asyncio.wait_for timeout cancels the await but not the thread.
            matched = await asyncio.wait_for(
                asyncio.get_running_loop().run_in_executor(
                    None,
                    lambda: [
                        (i + 1, line) for i, line in enumerate(all_lines) if pat.search(line[:1000])
                    ],
                ),
                timeout=10.0,
            )
        except TimeoutError:
            return error("Regex search timed out (pattern may be too complex)")
        ctx_n = max(0, min(context, 10))
        if ctx_n > 0 and matched:
            # Build merged context blocks — deduplicate overlapping ranges
            ranges: list[tuple[int, int]] = []
            for n, _text in matched[:50]:
                start = max(0, n - 1 - ctx_n)
                end = min(total, n + ctx_n)
                # Merge with previous range if overlapping or adjacent
                if ranges and start <= ranges[-1][1]:
                    ranges[-1] = (ranges[-1][0], max(ranges[-1][1], end))
                else:
                    ranges.append((start, end))
            blocks = []
            for start, end in ranges:
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
        return _no_log_url_error(build, uuid)

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
        # Filter out parent directory, absolute links, and traversal entries
        entries = [
            e for e in entries if not e.startswith("/") and not e.startswith("http") and e != "../"
        ]
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
    skip_postrun: bool = True,
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
        skip_postrun: Skip post-run log collection lines and tail from the
                      end of the run phase instead (default true). Only
                      applies to job-output.txt. Set false to see raw tail.
    """
    uuid, t = _resolve(ctx, uuid, tenant, url, "build")
    build = await api(ctx, f"/tenant/{safepath(t)}/build/{safepath(uuid)}")
    log_url = build.get("log_url")
    if not log_url:
        return _no_log_url_error(build, uuid)
    if ".." in log_name.split("/"):
        return error(f"Invalid log_name: {log_name!r}")

    a = app(ctx)
    txt_url = log_url.rstrip("/") + "/" + log_name.lstrip("/")
    log_bytes, truncated = await stream_log(a, txt_url)
    raw = strip_ansi(log_bytes.decode("utf-8", errors="replace"))
    all_lines = raw.splitlines()
    total = len(all_lines)
    n = max(1, min(lines, 500))

    # Find the end of the run phase to skip post-run log collection
    run_end = total
    skipped_postrun = False
    if skip_postrun and log_name == "job-output.txt" and total > n:
        # Scan backwards for the "RUN END" marker (end of actual job)
        for i in range(total - 1, max(total - 2000, -1), -1):
            if _RUN_END_MARKER.search(all_lines[i]):
                run_end = i + 1  # include the marker line
                skipped_postrun = True
                break

    tail_start = max(0, run_end - n)
    tail = all_lines[tail_start:run_end]

    result_dict: dict[str, Any] = {
        "total_lines": total,
        "log_url": txt_url,
        "job": build.get("job_name", ""),
        "result": build.get("result", ""),
        "tail_from": tail_start + 1,
        "count": len(tail),
        "lines": [line[:500] for line in tail],
    }
    if skipped_postrun:
        result_dict["skipped_postrun"] = True
        result_dict["postrun_lines"] = total - run_end
    if truncated:
        result_dict["truncated"] = True
        result_dict["warning"] = (
            "Log exceeded 10 MB — tail is from truncated content, not the actual end"
        )
    return json.dumps(result_dict)

"""Log reading and browsing tools."""

import asyncio
import json
import re
from typing import Any
from urllib.parse import unquote, urlparse

import httpx
from mcp.server.mcpserver import Context

from ..errors import handle_errors
from ..helpers import (
    AppContext,
    api,
    app,
    clean,
    error,
    fetch_log_url,
    safepath,
    stream_log,
    strip_ansi,
    strip_zuul_timestamp,
)
from ..server import mcp
from ._common import (
    _ERROR_NOISE,
    _ERROR_PATTERNS,
    _MAX_FILE_BYTES,
    _MAX_LOG_LINES,
    _READ_ONLY,
    _RUN_END_MARKER,
    _decompress_gzip,
    _no_log_url_error,
    _resolve,
    _validate_log_url,
)

# Detect nested quantifiers that cause catastrophic backtracking (ReDoS).
# Matches patterns like (x+)+, (x*)+, (x+)*, (x{2,})+ etc.
_NESTED_QUANTIFIER_RE = re.compile(r"[+*}\?]\)?[+*{]")


async def _list_log_entries(a: AppContext, log_url: str) -> list[str]:
    """Best-effort directory listing for error messages."""
    try:
        resp = await fetch_log_url(a, log_url)
        if resp.status_code == 200 and "text/html" in resp.headers.get("content-type", ""):
            entries = re.findall(r'href="([^"?][^"]*)"', resp.text)
            return [
                e
                for e in entries
                if not e.startswith("/") and not e.startswith("http") and e != "../"
            ]
    except Exception:
        pass
    return []


async def _stream_log_with_fallback(
    a: AppContext, url: str, log_name: str, log_url: str = ""
) -> tuple[bytes, bool]:
    """Stream a log file with .gz fallback and automatic gzip decompression.

    1. Tries to fetch the exact URL.
    2. On 404, if log_name doesn't already end in .gz, retries with .gz appended.
    3. Detects gzip magic bytes and decompresses (handles file-level .gz
       compression, as opposed to HTTP Content-Encoding handled by httpx).
    4. On final 404, includes available files from the log directory in the error.

    Returns (content_bytes, truncated_bool).
    """
    found = False
    try:
        log_bytes, truncated = await stream_log(a, url)
        found = True
    except FileNotFoundError:
        if not log_name.endswith(".gz"):
            try:
                log_bytes, truncated = await stream_log(a, url + ".gz")
                found = True
            except FileNotFoundError:
                pass
    if not found:
        available = await _list_log_entries(a, log_url) if log_url else []
        # @handle_errors wraps FileNotFoundError as "Log file not found at {e}",
        # so pass just the name + hint, not a full sentence.
        msg = log_name
        if available:
            msg += f". Available: {', '.join(available[:10])}"
        raise FileNotFoundError(msg)
    try:
        log_bytes, gz_truncated = _decompress_gzip(log_bytes)
    except ValueError:
        # File-level corrupted gzip (magic bytes present but data invalid).
        # Re-raise as DecodingError so @handle_errors produces the helpful
        # message pointing users to diagnose_build.
        raise httpx.DecodingError("File-level gzip decompression failed") from None
    return log_bytes, truncated or gz_truncated


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
    direct_log_url: str = "",
    max_matches: int = 50,
    filter_noise: bool = True,
) -> str:
    """Read, search, and navigate build log files with grep, line ranges, and error summary.

    Args:
        uuid: Build UUID
        tenant: Tenant (default from env)
        log_name: Log file to read (default "job-output.txt")
        mode: "summary" (tail + errors), "errors" (errors only, no tail), or "full" (paginated)
        lines: For summary: tail count (default 50). For full: offset start line.
        start_line: Read from this line (1-based, overrides mode with end_line)
        end_line: Read up to this line (1-based, inclusive)
        grep: Regex to filter lines (overrides mode). Use | for OR.
        context: Lines of context around grep matches (default 0, max 10)
        url: Zuul build URL (alternative to uuid + tenant)
        direct_log_url: Log URL from a prior get_build/diagnose_build call.
                        Skips the build metadata fetch when provided.
        max_matches: Max grep matches to return (default 50, max 200)
        filter_noise: Filter out noise lines (failed=0, RETRYING) from grep results (default true)
    """
    log_url: str = ""
    if direct_log_url:
        _validate_log_url(direct_log_url)
        log_url = direct_log_url
        build: dict = {}
    else:
        uuid, t = _resolve(ctx, uuid, tenant, url, "build")
        build = await api(ctx, f"/tenant/{safepath(t)}/build/{safepath(uuid)}")
        log_url = build.get("log_url") or ""
    if not log_url:
        return _no_log_url_error(build, uuid)

    # Sanitize log_name to prevent path traversal (decode first to catch %2e%2e/%2f)
    if ".." in unquote(log_name).split("/"):
        return error(f"Invalid log_name: {log_name!r}")
    txt_url = log_url.rstrip("/") + "/" + log_name.lstrip("/")

    a = app(ctx)
    log_bytes, truncated = await _stream_log_with_fallback(a, txt_url, log_name, log_url)
    raw = strip_ansi(log_bytes.decode("utf-8", errors="replace"))
    all_lines = raw.splitlines()
    total = len(all_lines)

    # Line range mode (start_line/end_line)
    if start_line > 0:
        if start_line > total:
            return error(f"start_line {start_line} exceeds total {total} lines")
        if end_line > 0 and end_line < start_line:
            return error(f"end_line {end_line} is before start_line {start_line}")
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
    grep_note = ""
    if grep:
        # Auto-fix common shell-grep-to-python-regex mistake: \| -> |
        if r"\|" in grep and "|" not in grep.replace(r"\|", ""):
            original = grep
            grep = grep.replace(r"\|", "|")
            grep_note = f"Pattern auto-corrected from shell grep syntax: {original!r} -> {grep!r}"
        if _NESTED_QUANTIFIER_RE.search(grep):
            return error("Regex rejected: nested quantifiers can cause catastrophic backtracking")
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
                    a.grep_executor,
                    lambda: [
                        (i + 1, line) for i, line in enumerate(all_lines) if pat.search(line[:1000])
                    ],
                ),
                timeout=10.0,
            )
        except TimeoutError:
            return error("Regex search timed out (pattern may be too complex)")
        # Post-filter noise lines (failed=0, RETRYING) on main thread.
        # Skip filtering when the grep pattern explicitly targets noise
        # tokens — otherwise we'd silently remove what the user asked for.
        raw_count = len(matched)
        _NOISE_WORDS = ("retrying", "failed=0")
        g = grep.lower()
        grep_targets_noise = any(w in g for w in _NOISE_WORDS)
        if filter_noise and not grep_targets_noise:
            matched = [(n, t) for n, t in matched if not _ERROR_NOISE.search(t)]
        max_m = max(1, min(max_matches, 200))
        # Build O(1) match set for context output — avoids re-running the
        # user-supplied regex on the main asyncio thread (ReDoS protection).
        match_set: set[int] = {n - 1 for n, _ in matched}  # 1-based -> 0-based
        ctx_n = max(0, min(context, 10))
        if ctx_n > 0 and matched:
            # Build merged context blocks — deduplicate overlapping ranges
            ranges: list[tuple[int, int]] = []
            for n, _text in matched[:max_m]:
                start = max(0, n - 1 - ctx_n)
                end = min(total, n + ctx_n)
                if ranges and start <= ranges[-1][1]:
                    ranges[-1] = (ranges[-1][0], max(ranges[-1][1], end))
                else:
                    ranges.append((start, end))
            blocks = []
            for start, end in ranges:
                block = [
                    {
                        "n": i + 1,
                        "text": strip_zuul_timestamp(all_lines[i])[:500],
                        "match": i in match_set,
                    }
                    for i in range(start, end)
                ]
                blocks.append(block)
            result_dict = {
                "total_lines": total,
                "log_url": txt_url,
                "grep": grep,
                "matched": len(matched),
                "blocks": blocks,
            }
            if len(matched) > max_m:
                result_dict["truncated_matches"] = len(matched) - max_m
            if raw_count != len(matched):
                result_dict["noise_filtered"] = raw_count - len(matched)
            if grep_note:
                result_dict["grep_note"] = grep_note
            return json.dumps(result_dict)
        capped = matched[:max_m]
        result_dict = {
            "total_lines": total,
            "log_url": txt_url,
            "grep": grep,
            "matched": len(matched),
            "lines": [{"n": n, "text": strip_zuul_timestamp(text)[:500]} for n, text in capped],
        }
        if len(matched) > max_m:
            result_dict["truncated_matches"] = len(matched) - max_m
        if raw_count != len(matched):
            result_dict["noise_filtered"] = raw_count - len(matched)
        if grep_note:
            result_dict["grep_note"] = grep_note
        return json.dumps(result_dict)

    # Errors-only mode — just error lines, no tail
    if mode == "errors":
        errors: list[tuple[int, str]] = []
        for i, line in enumerate(all_lines):
            if _ERROR_PATTERNS.search(line) and not _ERROR_NOISE.search(line) and len(errors) < 30:
                errors.append((i + 1, line))
        err_result = {
            "total_lines": total,
            "log_url": txt_url,
            "error_count": len(errors),
            "error_lines": [{"n": n, "text": strip_zuul_timestamp(t)[:500]} for n, t in errors]
            or None,
        }
        if truncated:
            err_result["truncated"] = True
        return json.dumps(clean(err_result))

    # Summary mode — single pass for both errors and tail
    if mode == "summary":
        tail_n = max(1, lines) if lines else 50
        tail_start = max(0, total - tail_n)
        sum_errors: list[tuple[int, str]] = []
        tail: list[str] = []
        for i, line in enumerate(all_lines):
            if (
                _ERROR_PATTERNS.search(line)
                and not _ERROR_NOISE.search(line)
                and len(sum_errors) < 30
            ):
                sum_errors.append((i + 1, line))
            if i >= tail_start:
                tail.append(line)
        return json.dumps(
            clean(
                {
                    "total_lines": total,
                    "log_url": txt_url,
                    "job": build.get("job_name", "") or None,
                    "result": build.get("result", "") or None,
                    "error_lines": [
                        {"n": n, "text": strip_zuul_timestamp(t)[:500]} for n, t in sum_errors
                    ],
                    "tail": [strip_zuul_timestamp(line)[:500] for line in tail],
                }
            )
        )

    # Full mode (paginated)
    offset = max(0, lines)
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
    max_lines: int = 0,
    direct_log_url: str = "",
) -> str:
    """Browse or fetch files from a build's log directory.

    Without path: lists top-level. With trailing '/': lists subdirectory.
    With file path: fetches content (max 512KB). For filtered reads,
    use get_build_log with grep instead.

    Args:
        uuid: Build UUID
        tenant: Tenant (default from env)
        path: Relative path within the log dir (e.g. "logs/controller/")
        url: Zuul build URL (alternative to uuid + tenant)
        max_lines: Limit file content to first N lines (0 = no limit)
        direct_log_url: Log URL from a prior call. Skips build metadata fetch.
    """
    log_url: str = ""
    if direct_log_url:
        _validate_log_url(direct_log_url)
        log_url = direct_log_url
        build: dict = {}
    else:
        uuid, t = _resolve(ctx, uuid, tenant, url, "build")
        build = await api(ctx, f"/tenant/{safepath(t)}/build/{safepath(uuid)}")
        log_url = build.get("log_url") or ""
    if not log_url:
        return _no_log_url_error(build, uuid)

    parsed = urlparse(log_url)
    if parsed.scheme not in ("http", "https"):
        return error(f"Invalid log URL scheme: {parsed.scheme}")

    # Prevent path traversal (decode first to catch %2e%2e/%2f)
    if ".." in unquote(path).split("/"):
        return error("Path traversal not allowed")

    a = app(ctx)
    target_url = log_url.rstrip("/") + "/" + path.lstrip("/")

    # Cap download at 4x the file display limit — enough headroom for
    # gzip compression ratios while avoiding the 20 MB default that
    # wastes bandwidth when the output is capped at _MAX_FILE_BYTES.
    resp = await fetch_log_url(a, target_url, max_bytes=_MAX_FILE_BYTES * 4)
    if resp.status_code == 404:
        return error(f"Not found: {path or '/'}")
    resp.raise_for_status()

    content_type = resp.headers.get("content-type", "")

    # Directory listing (Apache/nginx index page).
    # Detect directories by: empty path, trailing slash, OR path without a file
    # extension (log servers redirect "logs" -> "logs/" but our path variable
    # still holds the user's original input without trailing slash).
    has_ext = bool(re.search(r"\.\w{1,10}$", path))
    if "text/html" in content_type and (not path or path.endswith("/") or not has_ext):
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

    # File content — decompress gzip if detected (same pattern as log tools)
    content, gz_truncated = _decompress_gzip(resp.content, _MAX_FILE_BYTES)
    raw = content[:_MAX_FILE_BYTES]
    try:
        text = raw.decode("utf-8", errors="replace")
    except Exception:
        return error(f"Cannot decode file at {path}")
    truncated = len(content) > _MAX_FILE_BYTES or gz_truncated

    result_dict: dict[str, Any] = {
        "log_url": target_url,
        "path": path,
        "size": len(content),
        "truncated": truncated,
    }

    if max_lines > 0:
        all_lines = text.splitlines()
        total = len(all_lines)
        limited = all_lines[:max_lines]
        result_dict["content"] = "\n".join(limited)
        result_dict["total_lines"] = total
        result_dict["lines_returned"] = len(limited)
        result_dict["has_more"] = len(limited) < total
    else:
        result_dict["content"] = text

    return json.dumps(result_dict)


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
    direct_log_url: str = "",
) -> str:
    """Get the last N lines of a build log — fastest way to see why a build failed.

    More token-efficient than get_build_log(mode="summary") when you
    just need the tail.

    Args:
        uuid: Build UUID
        tenant: Tenant (default from env)
        lines: Lines from the end (default 50, max 500)
        log_name: Log file to read (default "job-output.txt")
        url: Zuul build URL (alternative to uuid + tenant)
        skip_postrun: Tail from run phase end, skipping post-run (default true)
        direct_log_url: Log URL from a prior call. Skips build metadata fetch.
    """
    log_url: str = ""
    if direct_log_url:
        _validate_log_url(direct_log_url)
        log_url = direct_log_url
        build: dict = {}
    else:
        uuid, t = _resolve(ctx, uuid, tenant, url, "build")
        build = await api(ctx, f"/tenant/{safepath(t)}/build/{safepath(uuid)}")
        log_url = build.get("log_url") or ""
    if not log_url:
        return _no_log_url_error(build, uuid)
    if ".." in unquote(log_name).split("/"):
        return error(f"Invalid log_name: {log_name!r}")

    txt_url = log_url.rstrip("/") + "/" + log_name.lstrip("/")
    log_bytes, truncated = await _stream_log_with_fallback(app(ctx), txt_url, log_name, log_url)
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
        "job": build.get("job_name") or None,
        "result": build.get("result") or None,
        "tail_from": tail_start + 1,
        "count": len(tail),
        "lines": [strip_zuul_timestamp(line)[:500] for line in tail],
    }
    if skipped_postrun:
        result_dict["skipped_postrun"] = True
        result_dict["postrun_lines"] = total - run_end
    if truncated:
        result_dict["truncated"] = True
        result_dict["warning"] = (
            "Log exceeded 10 MB — tail is from truncated content, not the actual end"
        )
    return json.dumps(clean(result_dict))

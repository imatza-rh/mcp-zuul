"""Build and buildset tools."""

import asyncio
import json
import re
from typing import Any

import httpx
from mcp.server.mcpserver import Context

from ..classifier import Classification, classify_failure, determine_failure_phase
from ..errors import handle_errors
from ..formatters import fmt_build, fmt_buildset
from ..helpers import (
    api,
    app,
    clean,
    error,
    fetch_log_url,
    pick_client,
    safepath,
    stream_log,
    strip_ansi,
)
from ..helpers import tenant as _tenant
from ..parsers import _BROAD_ERROR_PATTERN, grep_log_context
from ..server import mcp
from ._common import (
    _READ_ONLY,
    TimeFilters,
    _apply_time_filters,
    _fetch_job_output,
    _no_log_url_error,
    _resolve,
)

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


_CONFIDENCE_RANK = {"low": 1, "medium": 2, "high": 3}


def _reflect_on_diagnosis(
    classification: Classification,
    build_result: str,
    log_text: str | None,
    failed_tasks: list[dict],
    playbooks: list[dict],
    log_context: list[list[dict]],
) -> tuple[Classification, dict]:
    """Second-pass investigation when initial diagnosis is inconclusive.

    Runs broader error pattern grep on cached log text and re-classifies.
    Returns (possibly_updated_classification, reflection_notes).
    """
    checked = ["job-output.json (structured playbook data)"]
    if log_context:
        checked.append("job-output.txt (fatal/FAILED grep)")
    else:
        checked.append("job-output.txt (unavailable or empty)")

    unchecked = ["inner container/pod logs", "alternate log files (e.g. syslog, journal)"]

    original = classification
    broader_context: list[list[dict]] = []
    if log_text:
        broader_context = grep_log_context(log_text, pattern=_BROAD_ERROR_PATTERN)

    if broader_context:
        checked.append("job-output.txt (broad error grep: Traceback/Exception/ERROR/timeout)")
        updated = classify_failure(
            result=build_result,
            failed_tasks=failed_tasks,
            playbooks=playbooks,
            log_context=broader_context,
        )
        original_rank = _CONFIDENCE_RANK.get(classification.confidence, 0)
        updated_rank = _CONFIDENCE_RANK.get(updated.confidence, 0)
        if updated.category != "UNKNOWN" and (
            classification.category == "UNKNOWN" or updated_rank > original_rank
        ):
            classification = updated
    else:
        checked.append("job-output.txt (broad error grep: no additional matches)")

    reflection = clean(
        {
            "checked": checked,
            "unchecked": unchecked,
            "broader_matches": len(broader_context) if broader_context else 0,
            "reclassified": (classification.category, classification.confidence)
            != (original.category, original.confidence),
        }
    )
    return classification, reflection


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
        tenant: Tenant (default from env)
        project: Project filter
        pipeline: Pipeline filter
        job_name: Job name filter
        change: Change number filter
        branch: Branch filter
        patchset: Patchset filter
        ref: Git ref filter
        result: Result filter (SUCCESS, FAILURE, TIMED_OUT, SKIPPED, etc.)
        completed_after: ISO 8601 lower bound on completion time
        completed_before: ISO 8601 upper bound on completion time
        started_after: ISO 8601 lower bound on start time
        started_before: ISO 8601 upper bound on start time
        limit: Max results, 1-100 (default 20)
        skip: Pagination offset
    """
    t = _tenant(ctx, tenant)
    limit = max(1, min(limit, 100))
    skip = max(0, skip)
    tf = TimeFilters(completed_after, completed_before, started_after, started_before)
    fetch_limit = tf.fetch_limit(limit)

    # When time filters are active, skip is applied client-side after filtering
    api_skip = 0 if tf.active else skip
    params: dict[str, Any] = {"limit": fetch_limit + 1, "skip": api_skip}
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
    api_returned_full = len(data) > fetch_limit
    data = _apply_time_filters(data, tf)

    if tf.active and skip:
        data = data[skip:]

    has_more = len(data) > limit or (tf.active and api_returned_full)
    builds = [fmt_build(b) for b in data[:limit]]
    out: dict[str, Any] = {"builds": builds, "count": len(builds), "has_more": has_more}
    # Progressive summary: result counts + latest failure UUID.
    # Helps LLMs decide whether to drill deeper without parsing
    # individual builds.
    if builds:
        counts: dict[str, int] = {}
        latest_failure_uuid = None
        for b in builds:
            r = b.get("result", "UNKNOWN")
            counts[r] = counts.get(r, 0) + 1
            if not latest_failure_uuid and r in (
                "FAILURE", "POST_FAILURE", "TIMED_OUT", "NODE_FAILURE",
            ):
                latest_failure_uuid = b.get("uuid")
        out["result_counts"] = counts
        if latest_failure_uuid:
            out["latest_failure_uuid"] = latest_failure_uuid
    return json.dumps(out)


@mcp.tool(title="Build Details", annotations=_READ_ONLY)
@handle_errors
async def get_build(
    ctx: Context,
    uuid: str = "",
    tenant: str = "",
    url: str = "",
    brief: bool = False,
) -> str:
    """Get build details — log URL, nodeset, artifacts, timing, error detail.

    Args:
        uuid: Build UUID (full or prefix from list_builds)
        tenant: Tenant (default from env)
        url: Zuul build URL (alternative to uuid + tenant)
        brief: Compact response with just uuid, job, result, pipeline,
            duration (default false)
    """
    uuid, t = _resolve(ctx, uuid, tenant, url, "build")
    data = await api(ctx, f"/tenant/{safepath(t)}/build/{safepath(uuid)}")
    return json.dumps(fmt_build(data, brief=brief))


@mcp.tool(title="Build Failure Analysis", annotations=_READ_ONLY)
@handle_errors
async def get_build_failures(
    ctx: Context,
    uuid: str = "",
    tenant: str = "",
    url: str = "",
) -> str:
    """Analyze a failed build — which task failed, on which host, with error message.

    Parses job-output.json for precise failure data. Prefer diagnose_build
    for most use cases (adds classification and log context).

    Args:
        uuid: Build UUID
        tenant: Tenant (default from env)
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
                    "playbooks_passed": len(playbooks)
                    - len([p for p in playbooks if p.get("failed")]),
                    "playbooks": [p for p in playbooks if p.get("failed")],
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
    brief: bool = False,
) -> str:
    """One-call failure diagnosis — structured failures + relevant log context.

    Combines get_build_failures (which task failed, error message) with
    targeted log grep (surrounding context from job-output.txt). Returns
    everything needed to understand a failure in a single call.

    Use this instead of calling get_build_failures + get_build_log separately.

    Args:
        uuid: Build UUID
        tenant: Tenant (default from env)
        url: Zuul build URL (alternative to uuid + tenant)
        brief: Return only classification + root cause (default false).
               Omits playbooks, log_context, and full task details for ~95% smaller response.
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
    async def _fetch_log_text() -> tuple[str | None, bool]:
        try:
            log_bytes, trunc = await stream_log(app(ctx), log_url.rstrip("/") + "/job-output.txt")
            return strip_ansi(log_bytes.decode("utf-8", errors="replace")), trunc
        except Exception:
            return None, False

    async def _check_fetch_output_log() -> bool:
        try:
            a = app(ctx)
            target = log_url.rstrip("/") + "/fetch-output.log"
            resp = await pick_client(a, target).head(target, follow_redirects=True)
            return resp.status_code == 200
        except Exception:
            return False

    coros: list = [_fetch_job_output(ctx, log_url), _fetch_log_text()]
    if not brief:
        coros.append(_check_fetch_output_log())
    results = await asyncio.gather(*coros)
    (playbooks, failed_tasks, _json_ok) = results[0]
    (log_text, log_truncated) = results[1]
    has_fetch_log = results[2] if len(results) > 2 else False
    log_context = grep_log_context(log_text) if log_text else []

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

    # --- 4. Reflection: second-pass investigation for inconclusive results ---
    reflection: dict | None = None
    needs_reflection = classification and (
        classification.category == "UNKNOWN" or classification.confidence in ("low", "medium")
    )
    if needs_reflection:
        classification, reflection = _reflect_on_diagnosis(
            classification,  # type: ignore[arg-type]
            build_result=result,
            log_text=log_text,
            failed_tasks=failed_tasks,
            playbooks=playbooks,
            log_context=log_context,
        )

    # Extract node name from nodeset for SSH debugging
    nodeset = build.get("nodeset")
    node_name: str | None = None
    if isinstance(nodeset, dict):
        nodes = nodeset.get("nodes", [])
        if nodes and isinstance(nodes[0], dict):
            node_name = nodes[0].get("name")
    elif isinstance(nodeset, str) and nodeset:
        node_name = nodeset

    if brief:
        # Brief mode: classification + root cause only (~95% smaller)
        out: dict = {
            "job": build.get("job_name", ""),
            "result": result,
            "log_url": log_url,
            "duration": build.get("duration"),
            "failure_phase": failure_phase,
            "run_phase_passed": run_phase_passed,
        }
        if classification:
            out["classification"] = classification.category
            out["classification_reason"] = classification.reason
            out["classification_confidence"] = classification.confidence
            out["retryable"] = classification.retryable
        if failed_tasks:
            # When multiple phases failed ("mixed"), use the first failed task
            # (from the earliest phase — run before post in parse_playbooks order).
            # When single phase, use the last task (play-killer in block/rescue).
            root = failed_tasks[0] if failure_phase == "mixed" else failed_tasks[-1]
            rescued = root.get("rescued_count") or 0
            inner_count = len(root.get("inner_failures") or [])
            out["root_cause"] = clean(
                {
                    "task": root.get("task"),
                    "host": root.get("host"),
                    "msg": (root.get("msg") or "")[:500] or None,
                    "rc": root.get("rc"),
                    "rescued_count": rescued or None,
                    "inner_failures_note": (
                        f"{rescued} of {inner_count} inner failures were rescued "
                        "(handled by block/rescue)"
                    )
                    if rescued > 0
                    else None,
                }
            )
        if reflection:
            out["reflection"] = reflection
        if not failed_tasks and log_context:
            matches = [
                line["text"] for block in log_context[:2] for line in block if line.get("match")
            ]
            if matches:
                out["error_snippet"] = matches[0][:300]
        # Natural-language summary for direct LLM consumption
        duration = build.get("duration")
        dur_str = f" after {duration // 60}m" if duration and duration > 60 else ""
        job_name = build.get("job_name", "unknown")
        reason = (classification.reason if classification else "") or ""
        retry = " (retryable)" if classification and classification.retryable else ""
        out["summary"] = f"{job_name} {result}{dur_str}: {reason}{retry}".strip()
        return json.dumps(clean(out))

    out = {
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
        "playbooks_passed": len(playbooks) - len([p for p in playbooks if p.get("failed")]),
        "playbooks": [p for p in playbooks if p.get("failed")],
        "failed_tasks": failed_tasks,
        "log_context": log_context or None,
        "log_truncated": log_truncated or None,
        "failure_phase": failure_phase,
        "run_phase_passed": run_phase_passed,
        "reflection": reflection,
        "has_fetch_output_log": has_fetch_log or None,
    }

    if classification:
        out["classification"] = classification.category
        out["classification_reason"] = classification.reason
        out["classification_confidence"] = classification.confidence
        out["retryable"] = classification.retryable

    return json.dumps(clean(out))


@mcp.tool(title="Batch Diagnose Builds", annotations=_READ_ONLY)
@handle_errors
async def batch_diagnose(
    ctx: Context,
    uuids: list[str],
    tenant: str = "",
) -> str:
    """Classify multiple failed builds in one call — returns a triage table.

    Runs diagnose_build(brief=True) in parallel for each UUID and returns
    a compact classification summary. Use instead of calling diagnose_build
    N times when triaging multiple failures.

    Args:
        uuids: List of build UUIDs to diagnose (max 20)
        tenant: Tenant (default from env)
    """
    if not uuids:
        return error("uuids list is required")
    if len(uuids) > 20:
        return error("Maximum 20 UUIDs per call")

    sem = asyncio.Semaphore(5)

    async def _diagnose_one(uuid: str) -> dict:
        async with sem:
            try:
                result_json = await diagnose_build(ctx, uuid=uuid, tenant=tenant, brief=True)
                return json.loads(result_json)
            except Exception as exc:
                return {"uuid": uuid, "error": str(exc)}

    results = await asyncio.gather(*[_diagnose_one(u) for u in uuids])
    builds = list(results)

    summary: dict[str, int] = {}
    for b in builds:
        cat = b.get("classification") or b.get("result", "UNKNOWN")
        summary[cat] = summary.get(cat, 0) + 1

    return json.dumps({"builds": builds, "summary": summary, "count": len(builds)})


@mcp.tool(title="Diagnose with Tests", annotations=_READ_ONLY)
@handle_errors
async def diagnose_and_test(
    ctx: Context,
    uuid: str = "",
    tenant: str = "",
    url: str = "",
    brief: bool = True,
) -> str:
    """One-call diagnosis + test results — combines diagnose_build and get_build_test_results.

    Fetches build metadata once, then runs failure analysis and JUnit test
    parsing in parallel. Saves a round-trip vs calling both tools separately.

    Args:
        uuid: Build UUID
        tenant: Tenant (default from env)
        url: Zuul build URL (alternative to uuid + tenant)
        brief: Brief diagnosis (default true). Set false for full failure details.
    """
    from ._tests import _MAX_XML_BYTES, _find_test_xmls, _parse_junit_xml

    uuid, t = _resolve(ctx, uuid, tenant, url, "build")
    build = await api(ctx, f"/tenant/{safepath(t)}/build/{safepath(uuid)}")
    result = build.get("result", "")
    log_url = build.get("log_url")

    if result in ("SUCCESS", "SKIPPED"):
        msg = (
            "Build succeeded — nothing to diagnose."
            if result == "SUCCESS"
            else "Build was skipped."
        )
        short = clean({"job": build.get("job_name", ""), "result": result, "message": msg})
        return json.dumps({"diagnosis": short, "test_results": {"status": "skipped_for_success"}})

    if not log_url:
        return _no_log_url_error(build, uuid)

    a = app(ctx)
    base = log_url.rstrip("/")

    # --- Parallel: fetch job-output + test manifest ---
    async def _fetch_tests() -> dict:
        manifest_resp = await fetch_log_url(a, f"{base}/zuul-manifest.json")
        xml_paths: list[str] = []
        if manifest_resp.status_code == 200:
            try:
                manifest = manifest_resp.json()
                xml_paths = _find_test_xmls(manifest.get("tree", []))
            except Exception:
                pass
        if not xml_paths:
            return {"status": "no_tests"}
        sem = asyncio.Semaphore(5)

        async def _fetch_xml(path: str) -> tuple[str, httpx.Response | None]:
            async with sem:
                try:
                    return path, await fetch_log_url(a, f"{base}/{path}")
                except Exception:
                    return path, None

        xml_results = await asyncio.gather(*[_fetch_xml(p) for p in xml_paths[:10]])
        suites = []
        for xml_path, xml_resp in xml_results:
            if xml_resp is None or xml_resp.status_code != 200:
                continue
            content = xml_resp.content[:_MAX_XML_BYTES].decode("utf-8", errors="replace")
            parsed = _parse_junit_xml(content, xml_path)
            if parsed:
                suites.append(parsed)
        if not suites:
            return {"status": "no_tests"}
        totals = {"tests": 0, "passed": 0, "skipped": 0, "failed": 0, "errored": 0}
        for s in suites:
            for k in totals:
                totals[k] += s.get(k, 0)
        failed_suites = [s for s in suites if s.get("failed", 0) > 0 or s.get("errored", 0) > 0]
        if not failed_suites:
            return {"status": "all_passed", "suite_count": len(suites), "totals": totals}
        return {
            "status": "has_failures",
            "suite_count": len(suites),
            "totals": totals,
            "failed_suites": failed_suites,
        }

    async def _fetch_log_context() -> tuple[list[list[dict]], bool]:
        try:
            log_bytes, trunc = await stream_log(a, base + "/job-output.txt")
            return grep_log_context(strip_ansi(log_bytes.decode("utf-8", errors="replace"))), trunc
        except Exception:
            return [], False

    (
        (playbooks, failed_tasks, _json_ok),
        test_data,
        (log_context, _log_truncated),
    ) = await asyncio.gather(
        _fetch_job_output(ctx, log_url),
        _fetch_tests(),
        _fetch_log_context(),
    )

    # --- Build diagnosis ---
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
            run_phase_passed = not any(
                pb.get("phase") == "run" and pb.get("failed") for pb in playbooks
            )

    diag: dict[str, Any] = {
        "job": build.get("job_name", ""),
        "result": result,
        "log_url": log_url,
        "duration": build.get("duration"),
        "failure_phase": failure_phase,
        "run_phase_passed": run_phase_passed,
    }
    if classification:
        diag["classification"] = classification.category
        diag["classification_reason"] = classification.reason
        diag["classification_confidence"] = classification.confidence
        diag["retryable"] = classification.retryable
    if brief and failed_tasks:
        root = failed_tasks[0] if failure_phase == "mixed" else failed_tasks[-1]
        rescued = root.get("rescued_count") or 0
        inner_count = len(root.get("inner_failures") or [])
        diag["root_cause"] = clean(
            {
                "task": root.get("task"),
                "host": root.get("host"),
                "msg": (root.get("msg") or "")[:500] or None,
                "rc": root.get("rc"),
                "rescued_count": rescued or None,
                "inner_failures_note": (
                    f"{rescued} of {inner_count} inner failures were rescued "
                    "(handled by block/rescue)"
                )
                if rescued > 0
                else None,
            }
        )
    elif not brief:
        diag["playbooks"] = [p for p in playbooks if p.get("failed")]
        diag["failed_tasks"] = failed_tasks
        diag["log_context"] = log_context or None

    return json.dumps(clean({"diagnosis": clean(diag), "test_results": test_data}))


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
        tenant: Tenant (default from env)
        project: Project filter
        pipeline: Pipeline filter
        change: Change number filter
        branch: Branch filter
        ref: Git ref filter
        result: Result filter
        completed_after: ISO 8601 lower bound on completion time
        completed_before: ISO 8601 upper bound on completion time
        started_after: ISO 8601 lower bound on start time
        started_before: ISO 8601 upper bound on start time
        limit: Max results, 1-100 (default 20)
        skip: Pagination offset
        include_builds: Fetch full details per buildset (slower, best with limit <= 5)
    """
    t = _tenant(ctx, tenant)
    limit = max(1, min(limit, 100))
    skip = max(0, skip)
    tf = TimeFilters(completed_after, completed_before, started_after, started_before)
    fetch_limit = tf.fetch_limit(limit)

    api_skip = 0 if tf.active else skip
    params: dict[str, Any] = {"limit": fetch_limit + 1, "skip": api_skip}
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
    api_returned_full = len(data) > fetch_limit
    data = _apply_time_filters(
        data, tf, end_field="last_build_end_time", start_field="first_build_start_time"
    )

    if tf.active and skip:
        data = data[skip:]

    has_more = len(data) > limit or (tf.active and api_returned_full)
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
    brief: bool = True,
) -> str:
    """Get buildset details — result, pipeline, project, change.

    Brief (default): compact metadata only. Set brief=False for
    full details with builds, events, and timing.

    Args:
        uuid: Buildset UUID
        tenant: Tenant (default from env)
        url: Zuul buildset URL (alternative to uuid + tenant)
        brief: Compact response (default true). Set false for full timing/events.
    """
    uuid, t = _resolve(ctx, uuid, tenant, url, "buildset")
    data = await api(ctx, f"/tenant/{safepath(t)}/buildset/{safepath(uuid)}")
    return json.dumps(fmt_buildset(data, brief=brief))


@mcp.tool(title="Investigate Change", annotations=_READ_ONLY)
@handle_errors
async def investigate_change(
    ctx: Context,
    change: str,
    job: str = "",
    tenant: str = "",
) -> str:
    """One-call investigation — builds + diagnosis + autohold status for a change.

    Combines list_builds, diagnose_build(brief), and autohold lookup into
    a single response.  Designed for the common "what's happening with
    this change?" workflow — replaces 3-4 separate tool calls.

    Args:
        change: Change number (e.g. "2601")
        job: Job name filter (substring). When set, only builds matching
            this job are included and autoholds are filtered.
        tenant: Tenant (default from env)
    """
    if not change:
        raise ValueError("change is required")
    t = _tenant(ctx, tenant)

    # --- Parallel: builds + autoholds ---
    async def _fetch_builds() -> list:
        params: dict[str, Any] = {"change": change, "limit": 10}
        if job:
            params["job_name"] = job
        return await api(ctx, f"/tenant/{safepath(t)}/builds", params)

    async def _fetch_autoholds() -> list:
        data = await api(ctx, f"/tenant/{safepath(t)}/autohold")
        if job:
            return [a for a in data if job in (a.get("job") or "")]
        return data

    _DIAGNOSABLE = frozenset(
        {"FAILURE", "POST_FAILURE", "TIMED_OUT", "NODE_FAILURE", "DISK_FULL"}
    )

    try:
        builds_raw, autoholds_raw = await asyncio.gather(
            _fetch_builds(), _fetch_autoholds()
        )
    except Exception:
        builds_raw = await _fetch_builds()
        autoholds_raw = []

    builds = [fmt_build(b) for b in builds_raw]

    # --- Result summary ---
    result_counts: dict[str, int] = {}
    for b in builds:
        r = b.get("result", "UNKNOWN")
        result_counts[r] = result_counts.get(r, 0) + 1

    # --- Diagnose latest failure ---
    diagnosis: dict | None = None
    latest_failure = next(
        (b for b in builds_raw if b.get("result") in _DIAGNOSABLE),
        None,
    )
    if latest_failure and latest_failure.get("uuid"):
        try:
            diag_json = await diagnose_build(
                ctx, uuid=latest_failure["uuid"], tenant=tenant, brief=True
            )
            diagnosis = json.loads(diag_json)
        except Exception as exc:
            diagnosis = {"error": f"diagnosis failed: {type(exc).__name__}"}

    # --- Format autoholds ---
    relevant_autoholds = []
    for a in autoholds_raw:
        relevant_autoholds.append(
            clean(
                {
                    "id": a.get("id"),
                    "project": a.get("project"),
                    "job": a.get("job"),
                    "current_count": a.get("current_count"),
                    "max_count": a.get("max_count"),
                    "node_expiration": a.get("node_expiration"),
                    "expired": a.get("expired"),
                }
            )
        )

    # --- Build summary line ---
    total = len(builds)
    parts = [f"{v} {k}" for k, v in sorted(result_counts.items())]
    summary = f"{total} builds: {', '.join(parts)}" if parts else "no builds found"

    out: dict = {
        "change": change,
        "summary": summary,
        "result_counts": result_counts,
        "builds": builds,
    }
    if diagnosis:
        out["latest_failure_diagnosis"] = diagnosis
    if relevant_autoholds:
        out["autoholds"] = relevant_autoholds
    return json.dumps(clean(out))

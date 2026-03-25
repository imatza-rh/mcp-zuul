"""Token-efficient formatters for Zuul API responses."""

from __future__ import annotations

import time as _time

from .helpers import clean


def _format_duration(seconds: int | float | None) -> str | None:
    """Convert seconds to human-readable duration string.

    Returns "Xh Ym" for >=1h, "Xm Ys" for >=1m, "Xs" for <1m.
    Returns None if input is None or non-finite (inf/nan).
    """
    if seconds is None:
        return None
    try:
        seconds = max(0, int(seconds))
    except (OverflowError, ValueError):
        return None
    if seconds >= 3600:
        return f"{seconds // 3600}h {(seconds % 3600) // 60}m"
    if seconds >= 60:
        return f"{seconds // 60}m {seconds % 60}s"
    return f"{seconds}s"


def fmt_build(b: dict, brief: bool = True) -> dict:
    """Format a build response into a compact representation."""
    out: dict = {
        "uuid": b.get("uuid", "unknown"),
        "job": b.get("job_name", "unknown"),
        "result": b.get("result") or "IN_PROGRESS",
        "pipeline": b.get("pipeline"),
        "duration": b.get("duration"),
    }
    if not b.get("voting", True):
        out["voting"] = False
    ref = b.get("ref")
    ref_dict = ref if isinstance(ref, dict) else {}
    if ref_dict:
        out["project"] = ref_dict.get("project")
        out["change"] = ref_dict.get("change")
    if not brief:
        out["log_url"] = b.get("log_url")
        out["start_time"] = b.get("start_time")
        out["end_time"] = b.get("end_time")
        out["event_timestamp"] = b.get("event_timestamp")
        out["nodeset"] = b.get("nodeset")
        out["error_detail"] = b.get("error_detail")
        out["artifacts"] = [
            a.get("name", "") for a in b.get("artifacts", []) if isinstance(a, dict)
        ]
        out["ref_url"] = ref_dict.get("ref_url")
        out["patchset"] = ref_dict.get("patchset")
        out["branch"] = ref_dict.get("branch")
        bs = b.get("buildset")
        if bs:
            out["buildset_uuid"] = bs.get("uuid")
    return clean(out)


def fmt_buildset(bs: dict, brief: bool = True) -> dict:
    """Format a buildset response into a compact representation."""
    out: dict = {
        "uuid": bs.get("uuid", "unknown"),
        "result": bs.get("result") or "IN_PROGRESS",
        "pipeline": bs.get("pipeline"),
        "event_timestamp": bs.get("event_timestamp"),
    }
    refs = bs.get("refs", [])
    ref_dict = refs[0] if refs and isinstance(refs[0], dict) else {}
    if ref_dict:
        out["project"] = ref_dict.get("project")
        out["change"] = ref_dict.get("change")
    if not brief:
        out["message"] = bs.get("message")
        out["first_build_start"] = bs.get("first_build_start_time")
        out["last_build_end"] = bs.get("last_build_end_time")
        if ref_dict:
            out["ref_url"] = ref_dict.get("ref_url")
        if "builds" in bs:
            out["builds"] = [fmt_build(b) for b in bs["builds"]]
        if "events" in bs:
            out["events"] = bs["events"]
    return clean(out)


_TERMINAL_RESULTS = frozenset(
    {
        "SUCCESS",
        "FAILURE",
        "TIMED_OUT",
        "SKIPPED",
        "ABORTED",
        "RETRY_LIMIT",
        "NODE_FAILURE",
        "POST_FAILURE",
        "DISK_FULL",
        "CANCELED",
        "MERGER_FAILURE",
    }
)


def fmt_job_variants(data: list, description_limit: int = 200) -> list[dict]:
    """Format job API response into compact variant list."""
    variants = []
    for v in data:
        sc = v.get("source_context") or {}
        entry: dict = {
            "parent": v.get("parent"),
            "branches": v.get("branches", []) or None,
            "nodeset": v.get("nodeset"),
            "timeout": v.get("timeout"),
            "description": (v.get("description") or "")[:description_limit] or None,
            "source_project": sc.get("project"),
        }
        if not v.get("voting", True):
            entry["voting"] = False
        if v.get("abstract"):
            entry["abstract"] = True
        variants.append(clean(entry))
    return variants


def fmt_project(data: dict, name: str = "") -> dict:
    """Format project API response into compact representation."""
    configs: dict[str, list[str]] = {}
    for cfg in data.get("configs", []):
        for pip in cfg.get("pipelines", []):
            pname = pip.get("name", "")
            jobs = []
            for j in pip.get("jobs", []):
                if isinstance(j, list):
                    if j and isinstance(j[0], dict):
                        jobs.append(j[0].get("name", ""))
                    else:
                        jobs.append("")
                elif isinstance(j, dict):
                    jobs.append(j.get("name", ""))
            if jobs:
                configs[pname] = jobs
    return clean(
        {
            "project": name or data.get("canonical_name", ""),
            "canonical_name": data.get("canonical_name"),
            "connection": data.get("connection_name"),
            "type": data.get("type"),
            "pipelines": configs,
        }
    )


def _job_status(j: dict) -> str:
    """Compute a canonical status string for a job.

    Always returns one of: SUCCESS, FAILURE, TIMED_OUT, SKIPPED, ABORTED,
    RETRY_LIMIT, NODE_FAILURE, POST_FAILURE, RUNNING, WAITING, QUEUED.
    """
    result = j.get("result")
    if result:
        return result
    if j.get("waiting_status"):
        return "WAITING"
    if j.get("start_time"):
        return "RUNNING"
    return "QUEUED"


def _compute_chain_summary(jobs: list[dict]) -> dict:
    """Compute pipeline chain progress and critical-path ETA.

    Walks the dependency graph to find the longest remaining-work path.
    All times are in seconds. Returns numeric values internally;
    fmt_status_item converts to human-readable strings for output.
    """
    if not jobs:
        return {
            "completed": 0,
            "total": 0,
            "running": 0,
            "waiting": 0,
            "progress_pct": 0,
            "critical_path_remaining": 0,
            "all_decided": False,
        }

    completed = sum(1 for j in jobs if j.get("result"))
    running = sum(1 for j in jobs if j.get("status") == "RUNNING")
    waiting = sum(1 for j in jobs if j.get("status") in ("WAITING", "QUEUED"))
    total = len(jobs)
    progress_pct = round((completed / total) * 100)

    # Filter out jobs without a name — they can't participate in dependency resolution
    all_jobs = jobs  # preserve original list for all_decided check
    jobs = [j for j in jobs if j.get("name")]
    by_name: dict[str, dict] = {j["name"]: j for j in jobs}
    cache: dict[str, int | float] = {}
    visiting: set[str] = set()  # cycle detection

    def _remaining_through(name: str) -> int | float:
        if name in cache:
            return cache[name]
        if name in visiting:
            cache[name] = 0  # break cycle
            return 0
        job = by_name.get(name)
        if not job:
            cache[name] = 0
            return 0

        visiting.add(name)

        if job.get("result"):
            cache[name] = 0
            visiting.discard(name)
            return 0

        remaining = job.get("_remaining_secs")
        estimated = job.get("_estimated_secs", 0) or 0
        elapsed = job.get("_elapsed_secs", 0) or 0

        if job.get("status") == "RUNNING":
            if remaining is not None:
                own = max(0, remaining)  # clamp: overdue jobs have negative remaining
            else:
                own = max(0, estimated - elapsed)
        else:
            # WAITING/QUEUED — full estimated duration plus dependency wait
            deps = job.get("dependencies") or []
            dep_names = [d.get("name", "") if isinstance(d, dict) else d for d in deps]
            dep_max = max((_remaining_through(n) for n in dep_names), default=0) if deps else 0
            own = estimated + dep_max

        cache[name] = own
        visiting.discard(name)
        return own

    critical_path = int(max((_remaining_through(j["name"]) for j in jobs), default=0))

    # A job's outcome is "decided" when it has a terminal result or pre_fail.
    # all_decided=True means every job's fate is known even if some are still
    # running in post-run cleanup - callers can stop rapid-polling.
    all_decided = total > 0 and all(
        j.get("result") in _TERMINAL_RESULTS or j.get("pre_fail") for j in all_jobs
    )

    return {
        "completed": completed,
        "total": total,
        "running": running,
        "waiting": waiting,
        "progress_pct": progress_pct,
        "critical_path_remaining": critical_path,
        "all_decided": all_decided,
    }


def iter_status_items(pipelines: list, *, project: str = "", active_only: bool = True):
    """Yield (pipeline_name, item) pairs from Zuul status pipelines.

    Flattens the nested pipeline -> change_queues -> heads -> items
    structure into a simple iterator with optional project/active filtering.
    """
    for p in pipelines:
        for queue in p.get("change_queues", []):
            for heads_group in queue.get("heads", []):
                for item in heads_group:
                    if project:
                        item_projects = [r.get("project", "") for r in item.get("refs", [])]
                        if not any(project in proj for proj in item_projects):
                            continue
                    if active_only and not item.get("active", False):
                        continue
                    yield p.get("name", ""), item


def _format_job(j: dict, now: float) -> dict:
    """Format a single job from pipeline status into compact representation.

    Normalizes times to seconds, recomputing elapsed/remaining from
    start_time for running jobs (Zuul's values can be stale by minutes).
    Numeric times are stored in _-prefixed keys for chain_summary computation;
    human-readable strings are stored in the output keys.
    """
    elapsed = j.get("elapsed_time")
    start = j.get("start_time")
    remaining = j.get("remaining_time")
    estimated = j.get("estimated_time")

    # Always compute elapsed from start_time for running jobs.
    # Zuul's elapsed_time is a snapshot from the scheduler's last
    # status update and can be stale by minutes.  Remaining is
    # also stale (estimated*1000 - stale_elapsed), so recompute
    # both from start_time for consistency.
    if start and not j.get("result"):
        elapsed = max(0, int(now - start))  # seconds, clamped for clock skew
        if estimated is not None:
            remaining = max(0, estimated - elapsed)  # fresh remaining
        else:
            remaining = None
    else:
        if elapsed is not None:
            elapsed = elapsed // 1000  # ms -> seconds
        if remaining is not None:
            remaining = max(0, remaining // 1000)  # ms -> seconds

    out: dict = {
        "name": j.get("name", ""),
        "uuid": j.get("uuid"),
        "status": _job_status(j),
        "result": j.get("result"),
        # Human-readable time strings (primary output)
        "elapsed": _format_duration(elapsed),
        "remaining": _format_duration(remaining),
        "estimated": _format_duration(estimated),
        # Numeric seconds for chain_summary computation (stripped before output)
        "_elapsed_secs": elapsed,
        "_remaining_secs": remaining,
        "_estimated_secs": estimated,
        "report_url": j.get("report_url"),
        "stream_url": j.get("url"),
        "dependencies": j.get("dependencies") or None,
        "waiting_status": j.get("waiting_status"),
    }
    if not j.get("voting", True):
        out["voting"] = False
    if j.get("pre_fail"):
        out["pre_fail"] = True

    return clean(out)


def fmt_status_item(item: dict) -> dict:
    """Format a pipeline status item into a compact representation.

    Times are normalized to seconds. Each job includes a computed ``status``
    field (RUNNING, WAITING, QUEUED, SUCCESS, FAILURE, ...) and human-readable
    ``elapsed``/``remaining``. A ``chain_summary`` with critical-path
    ETA is added when jobs are present.
    """
    out: dict = {
        "id": item.get("id", ""),
        "active": item.get("active", False),
        "live": item.get("live", False),
    }
    refs = item.get("refs", [])
    if refs and isinstance(refs[0], dict):
        r = refs[0]
        out["project"] = r.get("project", "")
        out["change"] = r.get("change") or r.get("ref", "")
        out["url"] = r.get("url", "")
    enqueue_time = item.get("enqueue_time")
    if enqueue_time:
        out["enqueue_time"] = enqueue_time / 1000  # ms -> seconds
    zuul_ref = item.get("zuul_ref", "")
    if zuul_ref.startswith("Z"):
        out["buildset_uuid"] = zuul_ref[1:]

    formatted_jobs: list[dict] = []
    jobs = item.get("jobs", [])
    if jobs:
        now = _time.time()
        formatted_jobs = [_format_job(j, now) for j in jobs]

    # Compute chain summary using numeric _-prefixed fields
    summary = _compute_chain_summary(formatted_jobs)

    # Convert chain summary: replace numeric critical_path with human-readable cp_eta
    cp = summary.pop("critical_path_remaining", 0)
    summary["cp_eta"] = _format_duration(cp) or "0s"
    out["chain_summary"] = summary

    # Strip internal numeric fields from jobs before output
    for job in formatted_jobs:
        job.pop("_elapsed_secs", None)
        job.pop("_remaining_secs", None)
        job.pop("_estimated_secs", None)

    if formatted_jobs:
        out["jobs"] = formatted_jobs

    failing = item.get("failing_reasons", [])
    if failing:
        out["failing_reasons"] = failing
    return out

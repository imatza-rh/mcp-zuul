"""Token-efficient formatters for Zuul API responses."""

from __future__ import annotations

import time as _time

from .helpers import clean


def _format_duration(seconds: int | float | None) -> str | None:
    """Convert seconds to human-readable duration string.

    Returns "Xh Ym" for >=1h, "Xm Ys" for >=1m, "Xs" for <1m.
    Returns None if input is None.
    """
    if seconds is None:
        return None
    seconds = int(seconds)
    if seconds >= 3600:
        return f"{seconds // 3600}h {(seconds % 3600) // 60}m"
    if seconds >= 60:
        return f"{seconds // 60}m {seconds % 60}s"
    return f"{seconds}s"


def fmt_build(b: dict, brief: bool = True) -> dict:
    """Format a build response into a compact representation."""
    out = {
        "uuid": b.get("uuid", "unknown"),
        "job": b.get("job_name", "unknown"),
        "result": b.get("result") or "IN_PROGRESS",
        "pipeline": b.get("pipeline", ""),
        "duration": b.get("duration"),
        "voting": b.get("voting", True),
        "start_time": b.get("start_time"),
        "log_url": b.get("log_url"),
    }
    ref = b.get("ref") or {}
    if ref:
        out["project"] = ref.get("project", "")
        out["change"] = ref.get("change")
        out["ref_url"] = ref.get("ref_url", "")
    bs = b.get("buildset")
    if bs:
        out["buildset_uuid"] = bs.get("uuid")
    if not brief:
        out["end_time"] = b.get("end_time")
        out["event_timestamp"] = b.get("event_timestamp")
        out["nodeset"] = b.get("nodeset")
        out["error_detail"] = b.get("error_detail")
        out["artifacts"] = [a["name"] for a in b.get("artifacts", [])]
        out["patchset"] = ref.get("patchset")
        out["branch"] = ref.get("branch")
    return clean(out)


def fmt_buildset(bs: dict, brief: bool = True) -> dict:
    """Format a buildset response into a compact representation."""
    out = {
        "uuid": bs.get("uuid", "unknown"),
        "result": bs.get("result") or "IN_PROGRESS",
        "pipeline": bs.get("pipeline", ""),
        "event_timestamp": bs.get("event_timestamp"),
    }
    refs = bs.get("refs", [])
    if refs:
        r = refs[0]
        out["project"] = r.get("project", "")
        out["change"] = r.get("change")
        out["ref_url"] = r.get("ref_url", "")
    if not brief:
        out["message"] = bs.get("message")
        out["first_build_start"] = bs.get("first_build_start_time")
        out["last_build_end"] = bs.get("last_build_end_time")
        if "builds" in bs:
            out["builds"] = [fmt_build(b) for b in bs["builds"]]
        if "events" in bs:
            out["events"] = bs["events"]
    return clean(out)


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
    All times are in seconds.
    """
    if not jobs:
        return {
            "completed": 0,
            "total": 0,
            "running": 0,
            "waiting": 0,
            "progress_pct": 0,
            "critical_path_remaining": 0,
            "critical_path_remaining_str": "0s",
            "all_decided": False,
        }

    completed = sum(1 for j in jobs if j.get("result"))
    running = sum(1 for j in jobs if j.get("status") == "RUNNING")
    waiting = sum(1 for j in jobs if j.get("status") in ("WAITING", "QUEUED"))
    total = len(jobs)
    progress_pct = round((completed / total) * 100)

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

        remaining = job.get("remaining")
        estimated = job.get("estimated", 0) or 0
        elapsed = job.get("elapsed", 0) or 0

        if job.get("status") == "RUNNING":
            if remaining is not None:
                own = max(0, remaining)  # clamp: overdue jobs have negative remaining
            else:
                own = max(0, estimated - elapsed)
        else:
            # WAITING/QUEUED — full estimated duration plus dependency wait
            deps = job.get("dependencies") or []
            dep_max = max((_remaining_through(d) for d in deps), default=0) if deps else 0
            own = estimated + dep_max

        cache[name] = own
        visiting.discard(name)
        return own

    critical_path = int(max((_remaining_through(j["name"]) for j in jobs), default=0))

    # A job's outcome is "decided" when it has a terminal result or pre_fail.
    # all_decided=True means every job's fate is known even if some are still
    # running in post-run cleanup — callers can stop rapid-polling.
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
    all_decided = total > 0 and all(
        j.get("result") in _TERMINAL_RESULTS or j.get("pre_fail") for j in jobs
    )

    return {
        "completed": completed,
        "total": total,
        "running": running,
        "waiting": waiting,
        "progress_pct": progress_pct,
        "critical_path_remaining": critical_path,
        "critical_path_remaining_str": _format_duration(critical_path),
        "all_decided": all_decided,
    }


def fmt_status_item(item: dict) -> dict:
    """Format a pipeline status item into a compact representation.

    Times are normalized to seconds. Each job includes a computed ``status``
    field (RUNNING, WAITING, QUEUED, SUCCESS, FAILURE, ...) and human-readable
    ``elapsed_str``/``remaining_str``. A ``chain_summary`` with critical-path
    ETA is added when jobs are present.
    """
    out: dict = {
        "id": item.get("id", ""),
        "active": item.get("active", False),
        "live": item.get("live", False),
    }
    refs = item.get("refs", [])
    if refs:
        r = refs[0]
        out["project"] = r.get("project", "")
        out["change"] = r.get("change") or r.get("ref", "")
        out["url"] = r.get("url", "")
    enqueue_time = item.get("enqueue_time")
    if enqueue_time:
        out["enqueue_time"] = enqueue_time / 1000  # ms → seconds
    zuul_ref = item.get("zuul_ref", "")
    if zuul_ref.startswith("Z"):
        out["buildset_uuid"] = zuul_ref[1:]

    formatted_jobs: list[dict] = []
    jobs = item.get("jobs", [])
    if jobs:
        now = _time.time()
        for j in jobs:
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
                    elapsed = elapsed // 1000  # ms → seconds
                if remaining is not None:
                    remaining = max(0, remaining // 1000)  # ms → seconds

            status = _job_status(j)

            formatted_jobs.append(
                clean(
                    {
                        "name": j.get("name", ""),
                        "uuid": j.get("uuid"),
                        "status": status,
                        "result": j.get("result"),
                        "voting": j.get("voting", True),
                        "pre_fail": j.get("pre_fail"),
                        "elapsed": elapsed,
                        "elapsed_str": _format_duration(elapsed),
                        "remaining": remaining,
                        "remaining_str": _format_duration(remaining),
                        "estimated": estimated,
                        "start_time": start,
                        "report_url": j.get("report_url"),
                        "stream_url": j.get("url"),
                        "dependencies": j.get("dependencies") or None,
                        "waiting_status": j.get("waiting_status"),
                        "queued": j.get("queued"),
                        "tries": j.get("tries"),
                    }
                )
            )
        out["jobs"] = formatted_jobs

    out["chain_summary"] = _compute_chain_summary(formatted_jobs)

    failing = item.get("failing_reasons", [])
    if failing:
        out["failing_reasons"] = failing
    return out

"""Token-efficient formatters for Zuul API responses."""

import time as _time

from .helpers import clean


def fmt_build(b: dict, brief: bool = True) -> dict:
    """Format a build response into a compact representation."""
    out = {
        "uuid": b.get("uuid", "unknown"),
        "job": b["job_name"],
        "result": b.get("result") or "IN_PROGRESS",
        "pipeline": b.get("pipeline", ""),
        "duration": b.get("duration"),
        "voting": b.get("voting", True),
        "start_time": b.get("start_time"),
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
        out["log_url"] = b.get("log_url")
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


def fmt_status_item(item: dict) -> dict:
    """Format a pipeline status item into a compact representation."""
    out = {
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
        out["enqueue_time"] = enqueue_time
    zuul_ref = item.get("zuul_ref", "")
    if zuul_ref.startswith("Z"):
        out["buildset_uuid"] = zuul_ref[1:]
    jobs = item.get("jobs", [])
    if jobs:
        now = _time.time()
        formatted_jobs = []
        for j in jobs:
            elapsed = j.get("elapsed_time")
            start = j.get("start_time")
            # Compute elapsed server-side when Zuul doesn't provide it
            if elapsed is None and start and not j.get("result"):
                elapsed = int((now - start) * 1000)  # ms
            formatted_jobs.append(
                clean(
                    {
                        "name": j.get("name", ""),
                        "uuid": j.get("uuid"),
                        "result": j.get("result"),
                        "voting": j.get("voting", True),
                        "pre_fail": j.get("pre_fail"),
                        "elapsed": elapsed,
                        "remaining": j.get("remaining_time"),
                        "estimated": j.get("estimated_time"),
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
    failing = item.get("failing_reasons", [])
    if failing:
        out["failing_reasons"] = failing
    return out

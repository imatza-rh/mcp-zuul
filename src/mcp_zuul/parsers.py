"""Ansible job-output.json parsing and log text analysis.

Pure functions that extract structured failure data from Zuul build
artifacts. No I/O - callers fetch the data, parsers transform it.
"""

import re

from .helpers import clean, strip_ansi

_FATAL_PATTERN = re.compile(r"fatal:|FAILED!", re.IGNORECASE)
_PLAY_RECAP_RE = re.compile(r"PLAY RECAP \*+")


def smart_truncate(text: str, max_size: int = 4000) -> str | None:
    """Truncate long text keeping head and tail so failures are visible.

    Short text (<= max_size) is returned as-is.  For long text, keeps a
    small head (shows what ran) and a larger tail (shows the failure).
    """
    if not text:
        return None
    text = strip_ansi(text)
    if len(text) <= max_size:
        return text or None
    head = max_size // 4
    tail = max_size - head - 60  # room for the separator
    mid = len(text) - head - tail
    return f"{text[:head]}\n\n[... {mid} chars omitted ...]\n\n{text[-tail:]}"


def extract_inner_recap(text: str) -> str | None:
    """Extract the last PLAY RECAP block from embedded ansible output.

    For container exec tasks (podman_container_exec, command running
    ansible-playbook), the stdout contains a nested ansible run.  The
    PLAY RECAP at the end reveals which hosts failed.  Returns the last
    RECAP block found, or None.
    """
    if not text or "PLAY RECAP" not in text:
        return None
    cleaned = strip_ansi(text)
    lines = cleaned.splitlines()
    last_recap_idx = None
    for i, line in enumerate(lines):
        if _PLAY_RECAP_RE.search(line):
            last_recap_idx = i
    if last_recap_idx is None:
        return None
    recap_lines = [lines[last_recap_idx]]
    for j in range(last_recap_idx + 1, min(last_recap_idx + 20, len(lines))):
        line = lines[j].strip()
        if not line:
            break
        recap_lines.append(lines[j])
    return "\n".join(recap_lines)


def _truncate_invocation(module_args: dict | None, max_size: int = 4000) -> dict | None:
    """Extract replay-relevant fields from module invocation args, with size cap."""
    if not module_args or not isinstance(module_args, dict):
        return None
    relevant_keys = ("target", "chdir", "params", "cmd", "creates", "removes")
    relevant = {k: v for k, v in module_args.items() if k in relevant_keys and v is not None}
    if not relevant:
        return None
    for k, v in relevant.items():
        if isinstance(v, str) and len(v) > max_size:
            relevant[k] = v[:max_size] + "..."
    return relevant


def parse_playbooks(data: list) -> tuple[list[dict], list[dict]]:
    """Parse job-output.json into playbook summaries and failed task details.

    Returns (playbooks, failed_tasks). Passing playbooks are compact;
    failed playbooks include stats and full path.
    """
    playbooks = []
    failed_tasks = []
    for pb in data:
        phase = pb.get("phase", "")
        playbook = pb.get("playbook", "")
        stats = pb.get("stats", {})
        has_failure = any(s.get("failures", 0) > 0 for s in stats.values())

        if has_failure:
            pb_summary = clean(
                {
                    "phase": phase,
                    "playbook": playbook.split("/")[-1] if "/" in playbook else playbook,
                    "playbook_full": playbook,
                    "failed": True,
                    "stats": stats,
                }
            )
        else:
            pb_summary = {
                "phase": phase,
                "playbook": playbook.split("/")[-1] if "/" in playbook else playbook,
                "failed": False,
            }
        playbooks.append(pb_summary)

        if has_failure:
            for play in pb.get("plays", []):
                play_name = play.get("play", {}).get("name", "")
                for task in play.get("tasks", []):
                    task_info = task.get("task", {})
                    task_name = task_info.get("name", "")
                    duration = task_info.get("duration", {})
                    for host, res in task.get("hosts", {}).items():
                        if res.get("failed"):
                            raw_stdout = str(res.get("stdout", ""))
                            ft = clean(
                                {
                                    "play": play_name,
                                    "task": task_name,
                                    "host": host,
                                    "msg": smart_truncate(str(res.get("msg", ""))),
                                    "rc": res.get("rc"),
                                    "cmd": res.get("cmd"),
                                    "stderr": smart_truncate(str(res.get("stderr", ""))),
                                    "stdout": smart_truncate(raw_stdout),
                                    "inner_recap": extract_inner_recap(raw_stdout),
                                    "invocation": _truncate_invocation(
                                        res.get("invocation", {}).get("module_args")
                                    ),
                                    "duration": duration.get("end", ""),
                                    "playbook": playbook,
                                }
                            )
                            failed_tasks.append(ft)
    return playbooks, failed_tasks


def grep_log_context(text: str, *, context_lines: int = 5) -> list[list[dict]]:
    """Grep log text for fatal/FAILED lines and return context blocks."""
    all_lines = text.splitlines()
    total = len(all_lines)
    matched = [(i + 1, line) for i, line in enumerate(all_lines) if _FATAL_PATTERN.search(line)]
    if not matched:
        return []
    ranges: list[tuple[int, int]] = []
    for n, _text in matched[:30]:
        start = max(0, n - 1 - context_lines)
        end = min(total, n + context_lines)
        if ranges and start <= ranges[-1][1]:
            ranges[-1] = (ranges[-1][0], max(ranges[-1][1], end))
        else:
            ranges.append((start, end))
    blocks: list[list[dict]] = []
    for start, end in ranges[:10]:
        block = [
            {
                "n": i + 1,
                "text": all_lines[i][:500],
                "match": _FATAL_PATTERN.search(all_lines[i]) is not None,
            }
            for i in range(start, end)
        ]
        blocks.append(block)
    return blocks


# Backward-compatible aliases (tests and tools import underscore-prefixed names)
_smart_truncate = smart_truncate
_extract_inner_recap = extract_inner_recap
_parse_playbooks = parse_playbooks
_grep_log_context = grep_log_context

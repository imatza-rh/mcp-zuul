"""Failure classification for Zuul builds.

Classifies build failures into actionable categories based on error
patterns from failed tasks, log context, and build metadata.

Classifications:
    INFRA_FLAKE: Transient infrastructure issue, safe to retry.
    REAL_FAILURE: Deterministic code/config issue, needs investigation.
    CONFIG_ERROR: Zuul configuration problem, terminal.
    UNKNOWN: Cannot determine from available data.

Pattern source: production CI monitoring across Zuul deployments.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# --- Pattern definitions ---
# Each tuple: (compiled_regex, reason_template)
# Patterns are checked against msg, stderr, stdout of failed tasks.

_INFRA_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # SSH / connectivity
    (re.compile(r"UNREACHABLE!", re.IGNORECASE), "SSH unreachable (transient network)"),
    (
        re.compile(r"kex_exchange_identification|ssh_exchange_identification", re.IGNORECASE),
        "SSH key exchange failed (transient)",
    ),
    (
        re.compile(r"Connection reset by peer", re.IGNORECASE),
        "Connection reset (transient network)",
    ),
    (
        re.compile(r"Connection timed out", re.IGNORECASE),
        "Connection timed out (transient network)",
    ),
    (
        re.compile(r"Connection refused", re.IGNORECASE),
        "Connection refused (service not ready or transient)",
    ),
    # DNS
    (re.compile(r"Could not resolve host", re.IGNORECASE), "DNS resolution failure (transient)"),
    (
        re.compile(r"Name or service not known", re.IGNORECASE),
        "DNS resolution failure (transient)",
    ),
    (
        re.compile(r"Temporary failure in name resolution", re.IGNORECASE),
        "DNS resolution failure (transient)",
    ),
    # Provisioning
    (re.compile(r"Beaker provision failed", re.IGNORECASE), "Beaker provisioning failure"),
    (re.compile(r"foreman.*error", re.IGNORECASE), "Foreman provisioning error"),
    (re.compile(r"Power action failed", re.IGNORECASE), "IPMI power action failed"),
    (re.compile(r"Unable to reserve host", re.IGNORECASE), "No available hosts in pool"),
    # OOM / resource exhaustion
    (re.compile(r"OOMKilled", re.IGNORECASE), "Container OOMKilled (resource limits)"),
    (re.compile(r"Cannot allocate memory", re.IGNORECASE), "Memory allocation failed (OOM)"),
    (re.compile(r"qemu-kvm.*Killed", re.IGNORECASE), "VM process killed (hypervisor OOM)"),
    (re.compile(r"No space left on device", re.IGNORECASE), "Disk full"),
    (re.compile(r"disk full|insufficient disk", re.IGNORECASE), "Disk space exhausted"),
    # Container registry
    (re.compile(r"ImagePullBackOff|ErrImagePull", re.IGNORECASE), "Container image pull failure"),
    (re.compile(r"manifest unknown", re.IGNORECASE), "Container image tag not found"),
    (
        re.compile(r"toomanyrequests", re.IGNORECASE),
        "Registry rate limit (too many requests)",
    ),
    (re.compile(r"registry timeout", re.IGNORECASE), "Registry timeout"),
    (re.compile(r"failed to pull image", re.IGNORECASE), "Image pull failure"),
    # CRC
    (
        re.compile(r"crc.*(extract|start).*failed|crc.*stale", re.IGNORECASE),
        "CRC setup failure",
    ),
    # Libvirt / network
    (
        re.compile(r"network.*already exists|virbr.*conflict", re.IGNORECASE),
        "Libvirt network conflict (stale from previous run)",
    ),
    (
        re.compile(r"Address already in use.*dnsmasq", re.IGNORECASE),
        "dnsmasq port conflict",
    ),
    # Kubernetes infra
    (
        re.compile(r"no endpoints available", re.IGNORECASE),
        "Webhook/service has no endpoints (transient, e.g. MetalLB)",
    ),
    (
        re.compile(r"Liveness probe failed.*command timed out", re.IGNORECASE),
        "Liveness probe timeout (transient)",
    ),
    (
        re.compile(r"Back-off restarting failed container", re.IGNORECASE),
        "Container restart backoff (transient)",
    ),
    # Certificate / TLS
    (
        re.compile(r"certificate.*expired|x509.*certificate", re.IGNORECASE),
        "Certificate error (transient or clock skew)",
    ),
    # Subscription (when visible — usually hidden by no_log)
    (
        re.compile(r"subscription-manager.*error", re.IGNORECASE),
        "Subscription registration failure",
    ),
    # Ansible connectivity
    (
        re.compile(r"Failed to connect to the host via ssh", re.IGNORECASE),
        "Ansible SSH connection failure",
    ),
    (
        re.compile(r"networking mapper failed.*unreachable", re.IGNORECASE),
        "VM unreachable after OOM (networking mapper)",
    ),
]

_REAL_FAILURE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Config / variable errors
    (
        re.compile(r"AnsibleUndefinedVariable", re.IGNORECASE),
        "Undefined Ansible variable",
    ),
    (
        re.compile(r"'dict object' has no attribute", re.IGNORECASE),
        "Missing dict attribute (likely missing variable or stage)",
    ),
    # Deployment failures
    (
        re.compile(r"overcloud deploy.*FAILED|Heat Stack.*CREATE_FAILED", re.IGNORECASE),
        "TripleO overcloud deployment failure",
    ),
    (
        re.compile(r"Resource CREATE failed", re.IGNORECASE),
        "Heat resource creation failure",
    ),
    # Operator / CRD
    (
        re.compile(r"CRD.*not found|no matches for kind", re.IGNORECASE),
        "Missing CRD or operator not installed",
    ),
    # Disk layout
    (
        re.compile(r"ocp_layout_assertions.*failed|lists_intersect", re.IGNORECASE),
        "Disk layout overlap (LVMS vs Cinder PVs)",
    ),
    # Parse errors
    (
        re.compile(r"failed at splitting arguments.*unbalanced", re.IGNORECASE),
        "Ansible parse_kv error (complex shell in block scalar)",
    ),
    (
        re.compile(r"IndentationError.*unexpected indent", re.IGNORECASE),
        "Python indentation error in YAML block scalar",
    ),
]


@dataclass(frozen=True, slots=True)
class Classification:
    """Result of classifying a build failure."""

    category: str  # INFRA_FLAKE, REAL_FAILURE, CONFIG_ERROR, UNKNOWN
    reason: str  # Human-readable explanation
    confidence: str  # high, medium, low
    retryable: bool  # Whether automated retry is safe


def classify_failure(
    result: str,
    failed_tasks: list[dict[str, Any]],
    playbooks: list[dict[str, Any]],
    log_context: list[Any] | None = None,
) -> Classification:
    """Classify a build failure from structured diagnosis data.

    Args:
        result: Build result (FAILURE, TIMED_OUT, POST_FAILURE, etc.)
        failed_tasks: Failed task details from _parse_playbooks
        playbooks: Playbook summaries from _parse_playbooks
        log_context: Optional log grep context blocks
    """
    # TIMED_OUT with no failed tasks = infra flake (killed mid-execution)
    if result == "TIMED_OUT" and not failed_tasks:
        return Classification(
            category="INFRA_FLAKE",
            reason="Job timed out with no task failures (killed mid-execution)",
            confidence="high",
            retryable=True,
        )

    # POST_FAILURE — check if run phase passed
    if result == "POST_FAILURE":
        run_failed = any(pb.get("phase") == "run" and pb.get("failed") for pb in playbooks)
        if not run_failed:
            return Classification(
                category="INFRA_FLAKE",
                reason="Post-run failed but run phase passed (log collection issue)",
                confidence="high",
                retryable=True,
            )
        # Run phase also failed — classify the run-phase errors below

    # Match failed task error text against patterns
    all_text = _collect_error_text(failed_tasks)
    if log_context:
        all_text += " " + _collect_log_text(log_context)

    if all_text:
        # Check both pattern lists, then decide priority.
        # Real failure patterns are more specific (code/config bugs) and take
        # precedence over infra patterns when both match — retrying a real bug
        # wastes CI resources, so the conservative call is REAL_FAILURE.
        infra_reason: str | None = None
        for pattern, reason in _INFRA_PATTERNS:
            if pattern.search(all_text):
                infra_reason = reason
                break

        real_reason: str | None = None
        for pattern, reason in _REAL_FAILURE_PATTERNS:
            if pattern.search(all_text):
                real_reason = reason
                break

        if real_reason:
            return Classification(
                category="REAL_FAILURE",
                reason=real_reason,
                confidence="high",
                retryable=False,
            )
        if infra_reason:
            return Classification(
                category="INFRA_FLAKE",
                reason=infra_reason,
                confidence="high",
                retryable=True,
            )

    # Failed tasks exist but no pattern matched
    if failed_tasks:
        first = failed_tasks[0]
        task_name = first.get("task", "unknown task")
        msg = (first.get("msg") or "")[:100]
        # Use inner failure details for a more specific reason
        inner_list = first.get("inner_failures") or []
        if inner_list:
            inner = inner_list[0]
            inner_task = inner.get("task", "")
            inner_msg = (inner.get("msg") or inner.get("raw") or "")[:100]
            reason = f"Inner playbook: '{inner_task}' failed: {inner_msg}".rstrip()
            confidence = "medium"
        else:
            reason = f"Task '{task_name}' failed: {msg}".rstrip()
            confidence = "medium"
        return Classification(
            category="REAL_FAILURE",
            reason=reason,
            confidence=confidence,
            retryable=False,
        )

    # No failed tasks, no pattern match — classify by result code
    if result in ("TIMED_OUT", "NODE_FAILURE", "RETRY_LIMIT", "DISK_FULL"):
        return Classification(
            category="INFRA_FLAKE",
            reason=f"{result} (no structured failure data available)",
            confidence="medium" if result == "TIMED_OUT" else "high",
            retryable=True,
        )

    if result == "MERGER_FAILURE":
        return Classification(
            category="CONFIG_ERROR",
            reason="Merge conflict or missing dependency",
            confidence="high",
            retryable=False,
        )

    return Classification(
        category="UNKNOWN",
        reason="No failed tasks found and no error patterns matched",
        confidence="low",
        retryable=False,
    )


def determine_failure_phase(playbooks: list[dict[str, Any]]) -> str | None:
    """Determine which execution phase(s) failed.

    Returns:
        "pre-run", "run", "post-run", "mixed", or None if no failures.
    """
    failed_phases: set[str] = set()
    for pb in playbooks:
        if pb.get("failed"):
            phase = (pb.get("phase") or "").lower()
            if phase in ("pre", "setup"):
                failed_phases.add("pre-run")
            elif phase == "run":
                failed_phases.add("run")
            elif phase in ("post", "cleanup"):
                failed_phases.add("post-run")
            elif phase:
                failed_phases.add(phase)
            else:
                failed_phases.add("unknown")

    if not failed_phases:
        return None
    if len(failed_phases) == 1:
        return failed_phases.pop()
    return "mixed"


_MAX_ERROR_TEXT = 50_000  # Cap total text scanned by regex patterns


def _collect_error_text(failed_tasks: list[dict[str, Any]]) -> str:
    """Collect searchable text from failed task fields.

    Includes inner_failures and extracted_errors so the classifier can
    see root causes that would otherwise be lost to stdout truncation.
    """
    parts: list[str] = []
    size = 0

    def _add(val: Any, limit: int = 2000) -> None:
        nonlocal size
        if not val:
            return
        chunk = str(val)[:limit]
        parts.append(chunk)
        size += len(chunk)

    for t in failed_tasks:
        for field in ("msg", "stderr", "stdout"):
            _add(t.get(field))
            if size >= _MAX_ERROR_TEXT:
                return " ".join(parts)
        # Inner failures from nested ansible playbooks
        for inner in t.get("inner_failures") or []:
            for field in ("msg", "stderr_excerpt", "cmd", "raw"):
                _add(inner.get(field), 500)
        # Extracted errors from pre-truncation scan
        for err in t.get("extracted_errors") or []:
            _add(err, 500)
    return " ".join(parts)


def _collect_log_text(log_context: list[Any]) -> str:
    """Collect searchable text from log context blocks."""
    parts = []
    for block in log_context:
        if isinstance(block, list):
            for line in block:
                if isinstance(line, dict) and line.get("match"):
                    parts.append(str(line.get("text", ""))[:500])
    return " ".join(parts)

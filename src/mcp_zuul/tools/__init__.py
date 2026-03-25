"""Zuul MCP tool implementations - 36 tools (31 read-only + 4 write + 1 LogJuicer)."""

# Re-export symbols used by prompts.py and tests
# Re-export all tool functions for backward compatibility (tests import from mcp_zuul.tools)
from ._builds import (
    _extract_file_paths,
    _ref_meta,
    diagnose_build,
    get_build,
    get_build_failures,
    get_buildset,
    list_builds,
    list_buildsets,
)
from ._common import (
    _MAX_JSON_LOG_BYTES,
    _extract_inner_recap,
    _fetch_job_output,
    _no_log_url_error,
    _parse_playbooks,
    _smart_truncate,
)
from ._config import (
    get_components,
    get_config_errors,
    get_connections,
    get_freeze_job,
    get_freeze_jobs,
    get_job,
    get_project,
    get_tenant_info,
    list_autoholds,
    list_jobs,
    list_labels,
    list_nodes,
    list_pipelines,
    list_projects,
    list_semaphores,
)
from ._logjuicer import get_build_anomalies
from ._logs import browse_build_logs, get_build_log, tail_build_log
from ._status import (
    find_flaky_jobs,
    get_build_times,
    get_change_status,
    get_job_durations,
    get_status,
    list_tenants,
)
from ._tests import get_build_test_results
from ._write import autohold_create, autohold_delete, dequeue, enqueue

__all__ = [
    "_MAX_JSON_LOG_BYTES",
    "_extract_file_paths",
    "_extract_inner_recap",
    "_fetch_job_output",
    "_no_log_url_error",
    "_parse_playbooks",
    "_ref_meta",
    "_smart_truncate",
    "autohold_create",
    "autohold_delete",
    "browse_build_logs",
    "dequeue",
    "diagnose_build",
    "enqueue",
    "find_flaky_jobs",
    "get_build",
    "get_build_anomalies",
    "get_build_failures",
    "get_build_log",
    "get_build_test_results",
    "get_build_times",
    "get_buildset",
    "get_change_status",
    "get_components",
    "get_config_errors",
    "get_connections",
    "get_freeze_job",
    "get_freeze_jobs",
    "get_job",
    "get_job_durations",
    "get_project",
    "get_status",
    "get_tenant_info",
    "list_autoholds",
    "list_builds",
    "list_buildsets",
    "list_jobs",
    "list_labels",
    "list_nodes",
    "list_pipelines",
    "list_projects",
    "list_semaphores",
    "list_tenants",
    "tail_build_log",
]

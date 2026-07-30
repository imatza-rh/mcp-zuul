# Tools Reference

47 tools across 8 categories.

## Builds & Failures

| Tool | Description |
|------|-------------|
| `list_builds` | Search builds by project, pipeline, job, change, result. Includes `buildset_uuid` for cross-referencing. |
| `get_build` | Full build details — nodeset, log URL, artifacts, error detail. Accepts `url` or `uuid`. |
| `get_build_failures` | Structured task-level failure data from `job-output.json` — failed play, task, host, msg, rc, stderr/stdout. |
| `diagnose_build` | **One-call failure diagnosis.** Combines structured failures with targeted log context. Includes automatic reflection for inconclusive results. |
| `batch_diagnose` | Classify multiple failed builds in one call. Returns a triage table with classification summary. |
| `diagnose_and_test` | Combined diagnosis + JUnit test results in a single call. Saves a round-trip. |

## Logs

| Tool | Description |
|------|-------------|
| `get_build_log` | Read and search log files. Modes: `summary`, `full`, `grep`, `errors`, or exact line ranges. Supports `log_name` for any file in the build's log directory. |
| `tail_build_log` | Last N lines of a log (default 50, max 500). The fastest way to check why a build failed. |
| `browse_build_logs` | List log directory contents or fetch specific files. Max 512KB per file. |
| `stream_build_console` | Live console output from RUNNING builds via WebSocket. Optional — requires `pip install mcp-zuul[console]`. |

## Buildsets

| Tool | Description |
|------|-------------|
| `list_buildsets` | Search buildsets. Use `include_builds=true` to inline full build details. |
| `get_buildset` | Full buildset with all builds and events. |

## Pipeline & Status

| Tool | Description |
|------|-------------|
| `get_status` | Live pipeline status — queued and running items with job progress and ETA. |
| `get_change_status` | Status for a change/PR/MR. In pipeline: live jobs. Not in pipeline: latest completed buildset. |
| `list_pipelines` | All pipelines with trigger types. |

## Jobs & Projects

| Tool | Description |
|------|-------------|
| `list_tenants` | All tenants with project counts. |
| `list_jobs` | List jobs with optional name filter. |
| `get_job` | Job config — parent, nodeset, timeout, variants, source project. |
| `get_project` | Pipelines and jobs configured for a project. |
| `list_projects` | List all projects with optional name filter. |
| `get_config_errors` | Configuration errors, missing refs, broken configs. Check when jobs aren't running. |
| `get_freeze_jobs` | Resolved job dependency graph for a pipeline/project/branch. |
| `get_freeze_job` | Resolved job config after inheritance — final merged nodeset, playbooks, timeout. |
| `find_flaky_jobs` | Analyze build history for intermittent failures. Computes pass/fail rate and flaky flag. |
| `get_build_times` | Build duration trends with avg/min/max stats. |
| `get_job_durations` | Batch duration stats for multiple jobs in one call. |
| `check_health` | Test API connectivity and auth status. |
| `get_tenant_info` | Tenant capabilities — auth realms, websocket URL. |

## Infrastructure

| Tool | Description |
|------|-------------|
| `list_nodes` | Nodepool nodes with state, provider, and label. Includes state summary. |
| `list_labels` | Available nodepool labels. |
| `list_semaphores` | Resource locks with current holders and max capacity. |
| `list_autoholds` | Active autohold requests. |
| `get_autohold` | Full autohold details — held nodes, timing, project/job. |
| `list_providers` | Nodepool cloud providers with flavors and images. |
| `list_images` | Nodepool disk images with build status. |
| `list_system_events` | System events — config updates, reconfigurations. |
| `get_badge` | CI status badge URL (SVG) for READMEs. |
| `get_connections` | Source connections — Gerrit, GitHub, GitLab instances. |
| `get_components` | System components — schedulers, executors, mergers, web servers. |

## Write Operations

!!! warning
    Disabled by default (`ZUUL_READ_ONLY=true`). Set `ZUUL_READ_ONLY=false` to enable. Requires auth token or Kerberos.

| Tool | Description |
|------|-------------|
| `enqueue` | Enqueue a change or ref into a pipeline. |
| `promote` | Promote changes to the top of a pipeline queue. |
| `reenqueue_buildset` | Re-enqueue a buildset by reading project/pipeline/ref from a previous one. |
| `dequeue` | Remove a change or ref from a pipeline. |
| `autohold_create` | Create an autohold request — hold nodes after failure for debugging. |
| `autohold_delete` | Delete an autohold request. |

## Test Results & Log Analysis

| Tool | Description |
|------|-------------|
| `get_build_test_results` | Parse JUnit XML test results. Discovers files via `zuul-manifest.json`, returns structured pass/fail/skip counts. |
| `get_build_anomalies` | ML-based log anomaly detection via LogJuicer. Requires `LOGJUICER_URL`. |

## Prompts

| Prompt | Description |
|--------|-------------|
| `debug_build` | Pre-loads build details + failures, checks flaky signal, guides root cause analysis. |
| `compare_builds` | Two builds side-by-side for differential analysis. |
| `check_change` | Live pipeline status or latest results for a change. |

## Resources

| Resource | URI Pattern |
|----------|-------------|
| Build details | `zuul://{tenant}/build/{uuid}` |
| Job configuration | `zuul://{tenant}/job/{name}` |
| Project configuration | `zuul://{tenant}/project/{org}/{repo}` |

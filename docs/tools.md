# Tools Reference

Auto-generated from the registered MCP tools. Do not edit by hand.

| Tool | Mode | Description |
| --- | --- | --- |
| `autohold_create` | write | Create an autohold request — hold nodes after a job failure for debugging. |
| `autohold_delete` | write | Delete an autohold request. |
| `batch_diagnose` | read | Classify multiple failed builds in one call — returns a triage table. |
| `browse_build_logs` | read | Browse or fetch files from a build's log directory. |
| `check_health` | read | Test Zuul API connectivity and auth status. |
| `dequeue` | write | Remove a change or ref from a pipeline. |
| `diagnose_and_test` | read | One-call diagnosis + test results — combines diagnose_build and get_build_test_results. |
| `diagnose_build` | read | One-call failure diagnosis — structured failures + relevant log context. |
| `enqueue` | write | Enqueue a change or ref into a pipeline for testing. |
| `find_flaky_jobs` | read | Detect flaky jobs by analyzing recent build history for intermittent failures. |
| `get_autohold` | read | Get details of a specific autohold request. |
| `get_badge` | read | Get a status badge URL (SVG) for a project's latest buildset result. |
| `get_build` | read | Get build details — log URL, nodeset, artifacts, timing, error detail. |
| `get_build_anomalies` | read | Detect anomalous log lines using LogJuicer ML-based analysis. |
| `get_build_failures` | read | Analyze a failed build — which task failed, on which host, with error message. |
| `get_build_log` | read | Read, search, and navigate build log files with grep, line ranges, and error summary. |
| `get_build_test_results` | read | Parse JUnit XML test results from a build's log directory. |
| `get_build_times` | read | Build duration trends — compute avg/min/max to detect performance regressions. |
| `get_buildset` | read | Get buildset details — result, pipeline, project, change. |
| `get_change_status` | read | Pipeline status for a specific change or PR/MR. |
| `get_components` | read | Show Zuul system components — schedulers, executors, mergers, web servers. |
| `get_config_errors` | read | Get Zuul configuration errors — broken configs, missing refs, syntax errors. |
| `get_connections` | read | List configured source connections — Gerrit, GitHub, GitLab instances. |
| `get_freeze_job` | read | Get fully-resolved job configuration after inheritance. |
| `get_freeze_jobs` | read | Get the resolved job graph for a pipeline/project/branch. |
| `get_job` | read | Get job configuration — parent, nodeset, timeout, branches, and all variants. |
| `get_job_durations` | read | Get avg/min/max duration for multiple jobs in a single call. |
| `get_project` | read | Get project configuration — which pipelines and jobs are configured. |
| `get_status` | read | Live pipeline status showing what's currently queued/running. |
| `get_tenant_info` | read | Get tenant capabilities, auth config, and websocket URL. |
| `investigate_change` | read | One-call investigation — builds + diagnosis + autohold status for a change. |
| `list_autoholds` | read | List autohold requests — nodes held after failure for debugging. |
| `list_builds` | read | Search builds with filters. Returns compact build summaries. |
| `list_buildsets` | read | Search buildsets (groups of builds triggered by a single event). |
| `list_images` | read | List nodepool disk images with build status and upload artifacts. |
| `list_jobs` | read | List all jobs in a tenant. Optionally filter by name substring. |
| `list_labels` | read | List available nodepool labels (node types that jobs can request). |
| `list_nodes` | read | List nodepool nodes — available, in-use, or provisioning. |
| `list_pipelines` | read | List all pipelines with their trigger types. |
| `list_projects` | read | List all projects in a tenant. Optionally filter by name substring. |
| `list_providers` | read | List nodepool cloud providers with flavors and images. |
| `list_semaphores` | read | List semaphores — resource locks that limit concurrent job execution. |
| `list_system_events` | read | List system events — config updates, reconfigurations, pipeline changes. |
| `list_tenants` | read | List all Zuul tenants with project and queue counts. |
| `promote` | write | Promote changes to the top of a pipeline queue. |
| `reenqueue_buildset` | write | Re-enqueue a buildset — re-triggers a previous buildset's pipeline run. |
| `stream_build_console` | read | Read live console output from a RUNNING build via WebSocket. |
| `tail_build_log` | read | Get the last N lines of a build log — fastest way to see why a build failed. |

---

Write tools are disabled by default with `ZUUL_READ_ONLY=true`.

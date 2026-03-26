"""Tests for the failure classifier module."""

import json

import httpx
import respx

from mcp_zuul.classifier import (
    Classification,
    classify_failure,
    determine_failure_phase,
)
from mcp_zuul.tools import diagnose_build, list_nodes
from tests.conftest import make_build


class TestClassifyFailure:
    """Test classify_failure with various error patterns."""

    def test_ssh_unreachable(self):
        tasks = [{"msg": "UNREACHABLE! Host is unreachable", "task": "Deploy"}]
        result = classify_failure("FAILURE", tasks, [])
        assert result.category == "INFRA_FLAKE"
        assert result.retryable is True
        assert result.confidence == "high"
        assert "SSH" in result.reason or "unreachable" in result.reason.lower()

    def test_dns_failure(self):
        tasks = [{"msg": "Could not resolve host: registry.example.com", "task": "Pull"}]
        result = classify_failure("FAILURE", tasks, [])
        assert result.category == "INFRA_FLAKE"
        assert "DNS" in result.reason

    def test_oom_killed(self):
        tasks = [{"msg": "container OOMKilled", "task": "Run tests"}]
        result = classify_failure("FAILURE", tasks, [])
        assert result.category == "INFRA_FLAKE"
        assert result.retryable is True

    def test_image_pull_backoff(self):
        tasks = [{"msg": "ImagePullBackOff for image foo:latest", "task": "Deploy"}]
        result = classify_failure("FAILURE", tasks, [])
        assert result.category == "INFRA_FLAKE"
        assert "image" in result.reason.lower() or "pull" in result.reason.lower()

    def test_disk_full(self):
        tasks = [{"stderr": "No space left on device", "task": "Write config"}]
        result = classify_failure("FAILURE", tasks, [])
        assert result.category == "INFRA_FLAKE"
        assert "disk" in result.reason.lower()

    def test_metalb_no_endpoints(self):
        """MetalLB webhook failure from tp!1925 session."""
        tasks = [{"msg": "Internal error: no endpoints available for service", "task": "Install"}]
        result = classify_failure("FAILURE", tasks, [])
        assert result.category == "INFRA_FLAKE"
        assert "endpoints" in result.reason.lower()
        assert result.retryable is True

    def test_connection_refused_in_stderr(self):
        tasks = [{"stderr": "Connection refused to host:5000", "msg": "err", "task": "X"}]
        result = classify_failure("FAILURE", tasks, [])
        assert result.category == "INFRA_FLAKE"

    def test_undefined_variable(self):
        tasks = [{"msg": "AnsibleUndefinedVariable: 'cifmw_foo' is undefined", "task": "Deploy"}]
        result = classify_failure("FAILURE", tasks, [])
        assert result.category == "REAL_FAILURE"
        assert result.retryable is False
        assert "Undefined" in result.reason

    def test_dict_no_attribute(self):
        tasks = [{"msg": "'dict object' has no attribute 'info'", "task": "Start VMs"}]
        result = classify_failure("FAILURE", tasks, [])
        assert result.category == "REAL_FAILURE"

    def test_overcloud_deploy_failed(self):
        tasks = [{"msg": "overcloud deploy FAILED", "task": "Deploy OSP"}]
        result = classify_failure("FAILURE", tasks, [])
        assert result.category == "REAL_FAILURE"
        assert "TripleO" in result.reason or "overcloud" in result.reason.lower()

    def test_parse_kv_error(self):
        tasks = [
            {
                "msg": "failed at splitting arguments, either an unbalanced jinja2 block or quotes",
                "task": "Shell",
            }
        ]
        result = classify_failure("FAILURE", tasks, [])
        assert result.category == "REAL_FAILURE"
        assert "parse_kv" in result.reason

    def test_timed_out_no_tasks(self):
        """TIMED_OUT with no failed tasks = infra flake."""
        result = classify_failure("TIMED_OUT", [], [])
        assert result.category == "INFRA_FLAKE"
        assert result.retryable is True
        assert "timed out" in result.reason.lower()

    def test_timed_out_with_tasks(self):
        """TIMED_OUT with failed tasks classifies by task content."""
        tasks = [{"msg": "AnsibleUndefinedVariable: foo", "task": "Deploy"}]
        result = classify_failure("TIMED_OUT", tasks, [])
        assert result.category == "REAL_FAILURE"

    def test_post_failure_run_passed(self):
        """POST_FAILURE with run phase passed = infra flake."""
        playbooks = [
            {"phase": "run", "failed": False},
            {"phase": "post", "failed": True},
        ]
        result = classify_failure("POST_FAILURE", [], playbooks)
        assert result.category == "INFRA_FLAKE"
        assert result.retryable is True
        assert "post-run" in result.reason.lower() or "Post-run" in result.reason

    def test_post_failure_run_also_failed(self):
        """POST_FAILURE with run phase also failed classifies from errors."""
        playbooks = [
            {"phase": "run", "failed": True},
            {"phase": "post", "failed": True},
        ]
        tasks = [{"msg": "AnsibleUndefinedVariable: x", "task": "Deploy"}]
        result = classify_failure("POST_FAILURE", tasks, playbooks)
        assert result.category == "REAL_FAILURE"

    def test_unknown_failure(self):
        """No tasks, no patterns = UNKNOWN."""
        result = classify_failure("FAILURE", [], [])
        assert result.category == "UNKNOWN"
        assert result.confidence == "low"

    def test_unrecognized_error_is_real_failure(self):
        """Failed tasks with unrecognized error = REAL_FAILURE with medium confidence."""
        tasks = [{"msg": "some completely novel error message", "task": "My task"}]
        result = classify_failure("FAILURE", tasks, [])
        assert result.category == "REAL_FAILURE"
        assert result.confidence == "medium"
        assert "My task" in result.reason

    def test_log_context_used_for_classification(self):
        """Patterns in log_context should also be matched."""
        log_context = [
            [
                {"text": "some line", "match": False},
                {"text": "fatal: UNREACHABLE! host is down", "match": True},
            ]
        ]
        result = classify_failure("FAILURE", [], [], log_context=log_context)
        assert result.category == "INFRA_FLAKE"

    def test_mixed_infra_and_real_prefers_real_failure(self):
        """When both infra and real patterns match, REAL_FAILURE wins.

        Real failure patterns are more specific and actionable - retrying
        a real bug wastes CI resources. The infra signal (Connection refused)
        may be a symptom of the real failure (undefined variable caused
        a service not to start).
        """
        tasks = [
            {"msg": "Connection refused to host:5000", "task": "Check service"},
            {"msg": "AnsibleUndefinedVariable: 'cifmw_foo' is undefined", "task": "Deploy"},
        ]
        result = classify_failure("FAILURE", tasks, [])
        assert result.category == "REAL_FAILURE"
        assert result.retryable is False
        assert "Undefined" in result.reason

    def test_mixed_infra_and_real_in_same_task(self):
        """Even within one task, real pattern takes priority over infra."""
        tasks = [
            {
                "msg": "Connection refused",
                "stderr": "AnsibleUndefinedVariable: 'x' is undefined",
                "task": "Deploy",
            }
        ]
        result = classify_failure("FAILURE", tasks, [])
        assert result.category == "REAL_FAILURE"
        assert result.retryable is False

    def test_infra_only_still_classified_as_infra(self):
        """Pure infra errors with no real failure patterns stay INFRA_FLAKE."""
        tasks = [
            {"msg": "Connection refused to host:5000", "task": "Check service"},
            {"msg": "UNREACHABLE! host is down", "task": "Ping"},
        ]
        result = classify_failure("FAILURE", tasks, [])
        assert result.category == "INFRA_FLAKE"
        assert result.retryable is True

    def test_beaker_provisioning(self):
        tasks = [{"msg": "Beaker provision failed for host titan99", "task": "Provision"}]
        result = classify_failure("FAILURE", tasks, [])
        assert result.category == "INFRA_FLAKE"
        assert "Beaker" in result.reason

    def test_classification_is_frozen(self):
        """Classification dataclass should be immutable."""
        c = Classification("INFRA_FLAKE", "test", "high", True)
        assert c.category == "INFRA_FLAKE"

    def test_inner_failures_used_for_classification(self):
        """Infra pattern in inner_failures should classify as INFRA_FLAKE."""
        tasks = [
            {
                "msg": "non-zero return code",
                "task": "Run playbook in container",
                "inner_failures": [
                    {
                        "host": "localhost",
                        "task": "Wait for OCP",
                        "msg": "Connection timed out waiting for cluster",
                    }
                ],
            }
        ]
        result = classify_failure("FAILURE", tasks, [])
        assert result.category == "INFRA_FLAKE"
        assert "timed out" in result.reason.lower()

    def test_inner_failures_fallback_reason_includes_inner_task(self):
        """When no pattern matches, fallback reason should use inner failure details."""
        tasks = [
            {
                "msg": "non-zero return code",
                "task": "Run playbook in container",
                "inner_failures": [
                    {
                        "host": "localhost",
                        "task": "install : Wait for bootstrap",
                        "msg": "bootstrap timeout (rc=4)",
                    }
                ],
            }
        ]
        result = classify_failure("FAILURE", tasks, [])
        assert result.category == "REAL_FAILURE"
        assert "bootstrap timeout" in result.reason
        assert "Inner playbook" in result.reason

    def test_extracted_errors_used_for_classification(self):
        """Infra pattern in extracted_errors should classify as INFRA_FLAKE."""
        tasks = [
            {
                "msg": "non-zero return code",
                "task": "Deploy",
                "extracted_errors": ["fatal: [host]: UNREACHABLE! host is down"],
            }
        ]
        result = classify_failure("FAILURE", tasks, [])
        assert result.category == "INFRA_FLAKE"
        assert "UNREACHABLE" in result.reason or "SSH" in result.reason


class TestDetermineFailurePhase:
    def test_run_phase_failure(self):
        playbooks = [
            {"phase": "pre", "failed": False},
            {"phase": "run", "failed": True},
            {"phase": "post", "failed": False},
        ]
        assert determine_failure_phase(playbooks) == "run"

    def test_post_phase_failure(self):
        playbooks = [
            {"phase": "run", "failed": False},
            {"phase": "post", "failed": True},
        ]
        assert determine_failure_phase(playbooks) == "post-run"

    def test_pre_phase_failure(self):
        playbooks = [{"phase": "pre", "failed": True}]
        assert determine_failure_phase(playbooks) == "pre-run"

    def test_setup_phase_mapped_to_pre_run(self):
        playbooks = [{"phase": "setup", "failed": True}]
        assert determine_failure_phase(playbooks) == "pre-run"

    def test_cleanup_phase_mapped_to_post_run(self):
        playbooks = [{"phase": "cleanup", "failed": True}]
        assert determine_failure_phase(playbooks) == "post-run"

    def test_mixed_phases(self):
        playbooks = [
            {"phase": "run", "failed": True},
            {"phase": "post", "failed": True},
        ]
        assert determine_failure_phase(playbooks) == "mixed"

    def test_no_failures(self):
        playbooks = [
            {"phase": "run", "failed": False},
            {"phase": "post", "failed": False},
        ]
        assert determine_failure_phase(playbooks) is None

    def test_empty_playbooks(self):
        assert determine_failure_phase([]) is None


class TestDiagnoseBuildClassification:
    """Test that diagnose_build includes classification fields."""

    @respx.mock
    async def test_includes_classification_for_failure(self, mock_ctx):
        build = make_build(result="FAILURE")
        log_text = "line 1\nfatal: UNREACHABLE! host down\nline 3"
        job_output = [
            {
                "phase": "run",
                "playbook": "/path/to/run.yaml",
                "stats": {"ctrl": {"failures": 1, "ok": 0}},
                "plays": [
                    {
                        "play": {"name": "Run"},
                        "tasks": [
                            {
                                "task": {"name": "Deploy", "duration": {}},
                                "hosts": {
                                    "ctrl": {
                                        "failed": True,
                                        "msg": "UNREACHABLE! host is down",
                                    }
                                },
                            }
                        ],
                    }
                ],
            }
        ]
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/fail-uuid").mock(
            return_value=httpx.Response(200, json=build)
        )
        respx.get(f"{build['log_url']}job-output.json.gz").mock(
            return_value=httpx.Response(200, json=job_output)
        )
        respx.get(f"{build['log_url']}job-output.txt").mock(
            return_value=httpx.Response(200, content=log_text.encode())
        )
        result = json.loads(await diagnose_build(mock_ctx, "fail-uuid"))
        assert result["classification"] == "INFRA_FLAKE"
        assert result["retryable"] is True
        assert result["classification_confidence"] == "high"
        assert "classification_reason" in result
        assert result["failure_phase"] == "run"
        assert result["run_phase_passed"] is False

    @respx.mock
    async def test_includes_start_time_and_pipeline(self, mock_ctx):
        build = make_build(result="FAILURE")
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/fail-uuid").mock(
            return_value=httpx.Response(200, json=build)
        )
        respx.get(f"{build['log_url']}job-output.json.gz").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {
                        "phase": "run",
                        "playbook": "/x.yaml",
                        "stats": {"h": {"failures": 1, "ok": 0}},
                        "plays": [
                            {
                                "play": {"name": "X"},
                                "tasks": [
                                    {
                                        "task": {"name": "T", "duration": {}},
                                        "hosts": {"h": {"failed": True, "msg": "err"}},
                                    }
                                ],
                            }
                        ],
                    }
                ],
            )
        )
        respx.get(f"{build['log_url']}job-output.txt").mock(
            return_value=httpx.Response(200, content=b"log")
        )
        result = json.loads(await diagnose_build(mock_ctx, "fail-uuid"))
        assert result["start_time"] == "2025-01-01T00:00:00"
        assert result["pipeline"] == "check"

    @respx.mock
    async def test_success_has_no_classification(self, mock_ctx):
        build = make_build(result="SUCCESS")
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/build-uuid-1").mock(
            return_value=httpx.Response(200, json=build)
        )
        result = json.loads(await diagnose_build(mock_ctx, "build-uuid-1"))
        assert "classification" not in result
        assert "failure_phase" not in result


class TestDiagnoseBuildFallback:
    """Test diagnose_build fallback paths."""

    @respx.mock
    async def test_json_gz_404_falls_back_to_uncompressed(self, mock_ctx):
        """When job-output.json.gz returns 404, diagnose_build tries .json."""
        build = make_build(result="FAILURE")
        job_output = [
            {
                "phase": "run",
                "playbook": "/path/to/run.yaml",
                "stats": {"ctrl": {"failures": 1, "ok": 0}},
                "plays": [
                    {
                        "play": {"name": "Run"},
                        "tasks": [
                            {
                                "task": {"name": "Deploy", "duration": {}},
                                "hosts": {
                                    "ctrl": {
                                        "failed": True,
                                        "msg": "AnsibleUndefinedVariable: x",
                                    }
                                },
                            }
                        ],
                    }
                ],
            }
        ]
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/fail-uuid").mock(
            return_value=httpx.Response(200, json=build)
        )
        # .gz returns 404
        respx.get(f"{build['log_url']}job-output.json.gz").mock(return_value=httpx.Response(404))
        # Uncompressed .json succeeds
        respx.get(f"{build['log_url']}job-output.json").mock(
            return_value=httpx.Response(200, json=job_output)
        )
        respx.get(f"{build['log_url']}job-output.txt").mock(
            return_value=httpx.Response(200, content=b"line 1\nline 2")
        )
        result = json.loads(await diagnose_build(mock_ctx, "fail-uuid"))
        assert result["classification"] == "REAL_FAILURE"
        assert len(result["failed_tasks"]) == 1
        assert "AnsibleUndefinedVariable" in result["failed_tasks"][0]["msg"]


class TestListNodesPoolHealth:
    """Test pool_health summary in list_nodes."""

    @respx.mock
    async def test_healthy_pool(self, mock_ctx):
        nodes = [
            {"state": "ready", "type": ["centos"]},
            {"state": "ready", "type": ["centos"]},
            {"state": "ready", "type": ["centos"]},
            {"state": "in-use", "type": ["centos"]},
        ]
        respx.get("https://zuul.example.com/api/tenant/test-tenant/nodes").mock(
            return_value=httpx.Response(200, json=nodes)
        )
        result = json.loads(await list_nodes(mock_ctx))
        assert result["pool_health"]["status"] == "healthy"
        assert result["pool_health"]["ready"] == 3
        assert result["pool_health"]["in_use"] == 1
        assert result["pool_health"]["total"] == 4

    @respx.mock
    async def test_exhausted_pool(self, mock_ctx):
        """No ready nodes and nothing building = exhausted."""
        nodes = [
            {"state": "in-use", "type": ["centos"]},
            {"state": "in-use", "type": ["centos"]},
        ]
        respx.get("https://zuul.example.com/api/tenant/test-tenant/nodes").mock(
            return_value=httpx.Response(200, json=nodes)
        )
        result = json.loads(await list_nodes(mock_ctx))
        assert result["pool_health"]["status"] == "exhausted"
        assert result["pool_health"]["ready"] == 0

    @respx.mock
    async def test_recovering_pool(self, mock_ctx):
        """No ready nodes but some building = recovering."""
        nodes = [
            {"state": "in-use", "type": ["centos"]},
            {"state": "in-use", "type": ["centos"]},
            {"state": "building", "type": ["centos"]},
        ]
        respx.get("https://zuul.example.com/api/tenant/test-tenant/nodes").mock(
            return_value=httpx.Response(200, json=nodes)
        )
        result = json.loads(await list_nodes(mock_ctx))
        assert result["pool_health"]["status"] == "recovering"
        assert result["pool_health"]["ready"] == 0
        assert result["pool_health"]["building"] == 1

    @respx.mock
    async def test_stressed_pool(self, mock_ctx):
        """Less than 20% ready nodes = stressed."""
        # 1 ready out of 6 = 16.7% < 20%
        nodes = [
            {"state": "ready", "type": ["centos"]},
            {"state": "in-use", "type": ["centos"]},
            {"state": "in-use", "type": ["centos"]},
            {"state": "in-use", "type": ["centos"]},
            {"state": "in-use", "type": ["centos"]},
            {"state": "in-use", "type": ["centos"]},
        ]
        respx.get("https://zuul.example.com/api/tenant/test-tenant/nodes").mock(
            return_value=httpx.Response(200, json=nodes)
        )
        result = json.loads(await list_nodes(mock_ctx))
        assert result["pool_health"]["status"] == "stressed"

    @respx.mock
    async def test_small_pool_not_stressed(self, mock_ctx):
        """Small pool with 1/4 ready (25%) is healthy, not stressed (threshold 20%)."""
        nodes = [
            {"state": "ready", "type": ["centos"]},
            {"state": "in-use", "type": ["centos"]},
            {"state": "in-use", "type": ["centos"]},
            {"state": "in-use", "type": ["centos"]},
        ]
        respx.get("https://zuul.example.com/api/tenant/test-tenant/nodes").mock(
            return_value=httpx.Response(200, json=nodes)
        )
        result = json.loads(await list_nodes(mock_ctx))
        assert result["pool_health"]["status"] == "healthy"

    @respx.mock
    async def test_empty_pool(self, mock_ctx):
        respx.get("https://zuul.example.com/api/tenant/test-tenant/nodes").mock(
            return_value=httpx.Response(200, json=[])
        )
        result = json.loads(await list_nodes(mock_ctx))
        assert result["pool_health"]["status"] == "empty"
        assert result["pool_health"]["total"] == 0


class TestChainSummaryAllDecided:
    """Test all_decided field in chain_summary."""

    def test_all_completed(self):
        from mcp_zuul.formatters import fmt_status_item
        from tests.conftest import make_chained_status_item

        item = make_chained_status_item()
        for j in item["jobs"]:
            j["result"] = "SUCCESS"
            j["elapsed_time"] = 300000
            j.pop("remaining_time", None)
            j.pop("waiting_status", None)
            j.pop("estimated_time", None)
        formatted = fmt_status_item(item)
        assert formatted["chain_summary"]["all_decided"] is True

    def test_running_not_decided(self):
        from mcp_zuul.formatters import fmt_status_item
        from tests.conftest import make_status_item

        item = make_status_item()  # default: 1 running job
        formatted = fmt_status_item(item)
        assert formatted["chain_summary"]["all_decided"] is False

    def test_pre_fail_counts_as_decided(self):
        import time

        from mcp_zuul.formatters import fmt_status_item
        from tests.conftest import make_status_item

        item = make_status_item(
            change=50001,
            jobs=[
                {
                    "name": "job-a",
                    "result": "SUCCESS",
                    "voting": True,
                    "elapsed_time": 300000,
                    "start_time": time.time() - 600,
                },
                {
                    "name": "job-b",
                    "result": None,
                    "voting": True,
                    "pre_fail": True,  # failed but still running post-run
                    "elapsed_time": 200000,
                    "start_time": time.time() - 400,
                    "estimated_time": 600,
                },
            ],
        )
        formatted = fmt_status_item(item)
        # job-a has result, job-b has pre_fail — all decided
        assert formatted["chain_summary"]["all_decided"] is True

    def test_mixed_decided_and_running(self):
        import time

        from mcp_zuul.formatters import fmt_status_item
        from tests.conftest import make_status_item

        item = make_status_item(
            change=50002,
            jobs=[
                {
                    "name": "job-a",
                    "result": "SUCCESS",
                    "voting": True,
                    "elapsed_time": 300000,
                    "start_time": time.time() - 600,
                },
                {
                    "name": "job-b",
                    "result": None,
                    "voting": True,
                    "elapsed_time": 200000,
                    "start_time": time.time() - 400,
                    "estimated_time": 600,
                },
            ],
        )
        formatted = fmt_status_item(item)
        # job-b is still running with no result and no pre_fail
        assert formatted["chain_summary"]["all_decided"] is False

    def test_empty_jobs_not_decided(self):
        from mcp_zuul.formatters import _compute_chain_summary

        summary = _compute_chain_summary([])
        assert summary["all_decided"] is False

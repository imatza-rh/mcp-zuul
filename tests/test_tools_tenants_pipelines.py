"""Integration tests for tenant, pipeline, job, and project tools."""

import json

import httpx
import respx

from mcp_zuul.tools import (
    find_flaky_jobs,
    get_config_errors,
    get_freeze_jobs,
    get_job,
    get_project,
    list_autoholds,
    list_jobs,
    list_labels,
    list_nodes,
    list_pipelines,
    list_projects,
    list_semaphores,
    list_tenants,
)


class TestListTenants:
    @respx.mock
    async def test_returns_tenants(self, mock_ctx):
        respx.get("https://zuul.example.com/api/tenants").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {"name": "tenant-a", "projects": 10, "queue": 5},
                    {"name": "tenant-b", "projects": 3, "queue": 0},
                ],
            )
        )
        result = json.loads(await list_tenants(mock_ctx))
        assert len(result) == 2
        assert result[0]["name"] == "tenant-a"
        assert result[0]["projects"] == 10

    @respx.mock
    async def test_api_error_returns_error_json(self, mock_ctx):
        respx.get("https://zuul.example.com/api/tenants").mock(
            return_value=httpx.Response(500, text="Internal Server Error")
        )
        result = json.loads(await list_tenants(mock_ctx))
        assert "error" in result
        assert "500" in result["error"]


class TestListPipelines:
    @respx.mock
    async def test_returns_pipelines_with_triggers(self, mock_ctx):
        respx.get("https://zuul.example.com/api/tenant/test-tenant/pipelines").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {"name": "check", "triggers": [{"driver": "gerrit"}]},
                    {"name": "gate", "triggers": [{"driver": "gerrit"}, {"driver": "timer"}]},
                ],
            )
        )
        result = json.loads(await list_pipelines(mock_ctx))
        assert result["count"] == 2
        assert result["pipelines"][0]["name"] == "check"
        assert result["pipelines"][1]["triggers"] == ["gerrit", "timer"]


class TestListJobs:
    @respx.mock
    async def test_returns_all_jobs(self, mock_ctx):
        respx.get("https://zuul.example.com/api/tenant/test-tenant/jobs").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {"name": "job-a", "description": "First job", "variants": [{}]},
                    {"name": "job-b", "description": None, "variants": [{}, {}]},
                ],
            )
        )
        result = json.loads(await list_jobs(mock_ctx))
        assert result["count"] == 2
        assert result["jobs"][1]["variants"] == 2

    @respx.mock
    async def test_filter_by_name(self, mock_ctx):
        respx.get("https://zuul.example.com/api/tenant/test-tenant/jobs").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {"name": "deploy-infra", "variants": [{}]},
                    {"name": "run-tests", "variants": [{}]},
                    {"name": "deploy-ocp", "variants": [{}]},
                ],
            )
        )
        result = json.loads(await list_jobs(mock_ctx, filter="deploy"))
        assert result["count"] == 2


class TestGetJob:
    @respx.mock
    async def test_returns_variants(self, mock_ctx):
        respx.get("https://zuul.example.com/api/tenant/test-tenant/job/my-job").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {
                        "parent": "base-job",
                        "branches": ["main"],
                        "nodeset": {"nodes": [{"name": "controller", "label": "centos-9"}]},
                        "timeout": 3600,
                        "voting": True,
                        "abstract": False,
                        "description": "My test job",
                        "source_context": {"project": "org/config"},
                    },
                ],
            )
        )
        result = json.loads(await get_job(mock_ctx, "my-job"))
        assert result["name"] == "my-job"
        assert len(result["variants"]) == 1
        assert result["variants"][0]["parent"] == "base-job"
        assert result["variants"][0]["source_project"] == "org/config"


class TestGetProject:
    @respx.mock
    async def test_returns_pipeline_jobs(self, mock_ctx):
        respx.get("https://zuul.example.com/api/tenant/test-tenant/project/org%2Frepo").mock(
            return_value=httpx.Response(
                200,
                json={
                    "canonical_name": "review.example.com/org/repo",
                    "connection_name": "gerrit",
                    "type": "config",
                    "configs": [
                        {
                            "pipelines": [
                                {"name": "check", "jobs": [{"name": "lint"}, {"name": "test"}]},
                                {"name": "gate", "jobs": [{"name": "deploy"}]},
                            ],
                        }
                    ],
                },
            )
        )
        result = json.loads(await get_project(mock_ctx, "org/repo"))
        assert result["project"] == "org/repo"
        assert result["pipelines"]["check"] == ["lint", "test"]
        assert result["pipelines"]["gate"] == ["deploy"]


class TestGetConfigErrors:
    @respx.mock
    async def test_returns_config_errors(self, mock_ctx):
        respx.get("https://zuul.example.com/api/tenant/test-tenant/config-errors").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {
                        "source_context": {
                            "project": "org/broken-repo",
                            "branch": "main",
                            "path": ".zuul.yaml",
                        },
                        "error": "Job 'missing-parent' not found",
                        "short_error": "Job not found",
                        "severity": "error",
                        "name": "Unknown",
                    },
                    {
                        "source_context": {"project": "org/other-repo"},
                        "error": "Repo access denied",
                        "short_error": "Access denied",
                        "severity": "warning",
                        "name": "Unknown",
                    },
                ],
            )
        )
        result = json.loads(await get_config_errors(mock_ctx))
        assert result["count"] == 2
        assert result["errors"][0]["project"] == "org/broken-repo"
        assert result["errors"][0]["severity"] == "error"
        assert result["errors"][0]["short_error"] == "Job not found"

    @respx.mock
    async def test_filter_by_project(self, mock_ctx):
        route = respx.get("https://zuul.example.com/api/tenant/test-tenant/config-errors").mock(
            return_value=httpx.Response(200, json=[])
        )
        await get_config_errors(mock_ctx, project="org/my-project")
        params = dict(route.calls[0].request.url.params)
        assert params["project"] == "org/my-project"

    @respx.mock
    async def test_empty_errors(self, mock_ctx):
        respx.get("https://zuul.example.com/api/tenant/test-tenant/config-errors").mock(
            return_value=httpx.Response(200, json=[])
        )
        result = json.loads(await get_config_errors(mock_ctx))
        assert result["count"] == 0
        assert result["errors"] == []

    @respx.mock
    async def test_error_text_truncated(self, mock_ctx):
        long_error = "E" * 1000
        respx.get("https://zuul.example.com/api/tenant/test-tenant/config-errors").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {
                        "source_context": {"project": "org/repo"},
                        "error": long_error,
                        "short_error": "short",
                        "severity": "error",
                    }
                ],
            )
        )
        result = json.loads(await get_config_errors(mock_ctx))
        assert len(result["errors"][0]["error"]) == 500


class TestListProjects:
    @respx.mock
    async def test_returns_all_projects(self, mock_ctx):
        respx.get("https://zuul.example.com/api/tenant/test-tenant/projects").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {
                        "name": "org/repo-a",
                        "connection_name": "github",
                        "canonical_name": "github.com/org/repo-a",
                        "type": "untrusted",
                    },
                    {
                        "name": "org/repo-b",
                        "connection_name": "gerrit",
                        "canonical_name": "gerrit.example.com/org/repo-b",
                        "type": "config",
                    },
                ],
            )
        )
        result = json.loads(await list_projects(mock_ctx))
        assert result["count"] == 2
        assert result["projects"][0]["name"] == "org/repo-a"
        assert result["projects"][0]["connection"] == "github"
        assert result["projects"][1]["type"] == "config"

    @respx.mock
    async def test_filter_by_name(self, mock_ctx):
        respx.get("https://zuul.example.com/api/tenant/test-tenant/projects").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {"name": "openstack/nova", "connection_name": "g", "type": "untrusted"},
                    {"name": "openstack/neutron", "connection_name": "g", "type": "untrusted"},
                    {"name": "ansible/zuul-jobs", "connection_name": "g", "type": "config"},
                ],
            )
        )
        result = json.loads(await list_projects(mock_ctx, filter="openstack"))
        assert result["count"] == 2
        assert all("openstack" in p["name"] for p in result["projects"])


class TestListNodes:
    @respx.mock
    async def test_returns_nodes_with_state_summary(self, mock_ctx):
        respx.get("https://zuul.example.com/api/tenant/test-tenant/nodes").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {
                        "id": "001",
                        "type": ["centos-9"],
                        "state": "in-use",
                        "provider": "cloud-a",
                        "connection_type": "ssh",
                        "external_id": "ext-1",
                    },
                    {
                        "id": "002",
                        "type": ["centos-9"],
                        "state": "ready",
                        "provider": "cloud-a",
                        "connection_type": "ssh",
                        "external_id": "ext-2",
                    },
                    {
                        "id": "003",
                        "type": ["ubuntu-22"],
                        "state": "in-use",
                        "provider": "cloud-b",
                        "connection_type": "ssh",
                        "external_id": "ext-3",
                    },
                ],
            )
        )
        result = json.loads(await list_nodes(mock_ctx))
        assert result["count"] == 3
        assert result["by_state"] == {"in-use": 2, "ready": 1}
        assert result["nodes"][0]["id"] == "001"

    @respx.mock
    async def test_empty_nodes(self, mock_ctx):
        respx.get("https://zuul.example.com/api/tenant/test-tenant/nodes").mock(
            return_value=httpx.Response(200, json=[])
        )
        result = json.loads(await list_nodes(mock_ctx))
        assert result["count"] == 0
        assert result["by_state"] == {}


class TestListLabels:
    @respx.mock
    async def test_returns_sorted_labels(self, mock_ctx):
        respx.get("https://zuul.example.com/api/tenant/test-tenant/labels").mock(
            return_value=httpx.Response(
                200,
                json=[{"name": "centos-9"}, {"name": "ubuntu-22"}, {"name": "alma-9"}],
            )
        )
        result = json.loads(await list_labels(mock_ctx))
        assert result["count"] == 3
        assert result["labels"] == ["alma-9", "centos-9", "ubuntu-22"]


class TestListSemaphores:
    @respx.mock
    async def test_returns_semaphores(self, mock_ctx):
        respx.get("https://zuul.example.com/api/tenant/test-tenant/semaphores").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {
                        "name": "deploy-lock",
                        "global": False,
                        "max": 1,
                        "holders": {"count": 1, "this_tenant": ["job-a"], "other_tenants": 0},
                    },
                    {
                        "name": "test-pool",
                        "global": True,
                        "max": 5,
                        "holders": {"count": 0, "this_tenant": [], "other_tenants": 0},
                    },
                ],
            )
        )
        result = json.loads(await list_semaphores(mock_ctx))
        assert result["count"] == 2
        assert result["semaphores"][0]["name"] == "deploy-lock"
        assert result["semaphores"][0]["holders_count"] == 1
        assert result["semaphores"][0]["holders"] == ["job-a"]


class TestListAutoholds:
    @respx.mock
    async def test_returns_autoholds(self, mock_ctx):
        respx.get("https://zuul.example.com/api/tenant/test-tenant/autohold").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {
                        "id": "1",
                        "project": "org/repo",
                        "job": "test-job",
                        "ref_filter": ".*",
                        "reason": "Debugging infra failure",
                        "count": 1,
                        "current_count": 0,
                        "max_count": 1,
                        "node_expiration": 86400,
                        "expired": False,
                    }
                ],
            )
        )
        result = json.loads(await list_autoholds(mock_ctx))
        assert result["count"] == 1
        assert result["autoholds"][0]["project"] == "org/repo"
        assert result["autoholds"][0]["job"] == "test-job"
        assert result["autoholds"][0]["expired"] is False

    @respx.mock
    async def test_empty_autoholds(self, mock_ctx):
        respx.get("https://zuul.example.com/api/tenant/test-tenant/autohold").mock(
            return_value=httpx.Response(200, json=[])
        )
        result = json.loads(await list_autoholds(mock_ctx))
        assert result["count"] == 0


class TestGetFreezeJobs:
    @respx.mock
    async def test_returns_job_graph(self, mock_ctx):
        respx.get(
            "https://zuul.example.com/api/tenant/test-tenant"
            "/pipeline/check/project/org%2Frepo/branch/main/freeze-jobs"
        ).mock(
            return_value=httpx.Response(
                200,
                json=[
                    {"name": "content-provider", "dependencies": []},
                    {
                        "name": "deploy-job",
                        "dependencies": [{"name": "content-provider", "soft": False}],
                    },
                ],
            )
        )
        result = json.loads(await get_freeze_jobs(mock_ctx, pipeline="check", project="org/repo"))
        assert result["count"] == 2
        assert result["pipeline"] == "check"
        assert result["jobs"][0]["name"] == "content-provider"
        assert result["jobs"][1]["dependencies"] == [{"name": "content-provider", "soft": False}]

    @respx.mock
    async def test_custom_branch(self, mock_ctx):
        route = respx.get(
            "https://zuul.example.com/api/tenant/test-tenant"
            "/pipeline/gate/project/org%2Frepo/branch/release-1.0/freeze-jobs"
        ).mock(return_value=httpx.Response(200, json=[]))
        await get_freeze_jobs(mock_ctx, pipeline="gate", project="org/repo", branch="release-1.0")
        assert route.called


class TestFindFlakyJobs:
    @respx.mock
    async def test_detects_flaky_job(self, mock_ctx):
        builds = [
            {"uuid": f"u{i}", "result": "SUCCESS" if i % 3 else "FAILURE", "duration": 100 + i}
            for i in range(10)
        ]
        respx.get("https://zuul.example.com/api/tenant/test-tenant/builds").mock(
            return_value=httpx.Response(200, json=builds)
        )
        result = json.loads(await find_flaky_jobs(mock_ctx, job_name="test-job"))
        assert result["analyzed"] == 10
        assert result["flaky"] is True
        assert result["failure_rate"] > 0
        assert "SUCCESS" in result["results"]
        assert "FAILURE" in result["results"]

    @respx.mock
    async def test_stable_job_not_flaky(self, mock_ctx):
        builds = [{"uuid": f"u{i}", "result": "SUCCESS", "duration": 100} for i in range(10)]
        respx.get("https://zuul.example.com/api/tenant/test-tenant/builds").mock(
            return_value=httpx.Response(200, json=builds)
        )
        result = json.loads(await find_flaky_jobs(mock_ctx, job_name="stable-job"))
        assert result["flaky"] is False
        assert result["failure_rate"] == 0.0

    @respx.mock
    async def test_consistently_failing_not_flaky(self, mock_ctx):
        builds = [{"uuid": f"u{i}", "result": "FAILURE", "duration": 50} for i in range(5)]
        respx.get("https://zuul.example.com/api/tenant/test-tenant/builds").mock(
            return_value=httpx.Response(200, json=builds)
        )
        result = json.loads(await find_flaky_jobs(mock_ctx, job_name="broken-job"))
        assert result["flaky"] is False
        assert result["failure_rate"] == 100.0

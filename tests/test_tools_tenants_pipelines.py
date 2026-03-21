"""Integration tests for tenant, pipeline, job, and project tools."""

import json

import httpx
import respx

from mcp_zuul.tools import (
    get_config_errors,
    get_job,
    get_project,
    list_jobs,
    list_pipelines,
    list_projects,
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

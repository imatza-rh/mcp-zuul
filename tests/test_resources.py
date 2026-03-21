"""Tests for MCP resource templates."""

import json

import httpx
import respx

from mcp_zuul.resources import build_resource, job_resource, project_resource
from tests.conftest import make_build


class TestBuildResource:
    @respx.mock
    async def test_returns_build_json(self, mock_ctx):
        build = make_build(uuid="res-uuid")
        respx.get("https://zuul.example.com/api/tenant/my-tenant/build/res-uuid").mock(
            return_value=httpx.Response(200, json=build)
        )
        result = json.loads(await build_resource(tenant="my-tenant", uuid="res-uuid", ctx=mock_ctx))
        assert result["uuid"] == "res-uuid"
        assert result["job"] == "test-job"
        assert "log_url" in result


class TestJobResource:
    @respx.mock
    async def test_returns_job_variants(self, mock_ctx):
        respx.get("https://zuul.example.com/api/tenant/my-tenant/job/deploy-job").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {
                        "parent": "base",
                        "branches": ["main"],
                        "nodeset": {"nodes": [{"name": "ctrl", "label": "centos-9"}]},
                        "timeout": 3600,
                        "voting": True,
                        "description": "Deploy to staging",
                        "source_context": {"project": "org/config"},
                    }
                ],
            )
        )
        result = json.loads(await job_resource(tenant="my-tenant", name="deploy-job", ctx=mock_ctx))
        assert result["name"] == "deploy-job"
        assert len(result["variants"]) == 1
        assert result["variants"][0]["parent"] == "base"


class TestProjectResource:
    @respx.mock
    async def test_returns_project_pipelines(self, mock_ctx):
        respx.get("https://zuul.example.com/api/tenant/my-tenant/project/org%2Frepo").mock(
            return_value=httpx.Response(
                200,
                json={
                    "canonical_name": "github.com/org/repo",
                    "connection_name": "github",
                    "type": "untrusted",
                    "configs": [
                        {
                            "pipelines": [
                                {"name": "check", "jobs": [{"name": "lint"}, {"name": "test"}]},
                            ]
                        }
                    ],
                },
            )
        )
        result = json.loads(
            await project_resource(tenant="my-tenant", name="org/repo", ctx=mock_ctx)
        )
        assert result["project"] == "org/repo"
        assert result["pipelines"]["check"] == ["lint", "test"]

"""Tests for write operations (enqueue, dequeue, autohold, enqueue_ref, reenqueue_buildset)."""

import json

import httpx
import respx

from mcp_zuul.tools import (
    autohold_create,
    autohold_delete,
    dequeue,
    enqueue,
    enqueue_ref,
    reenqueue_buildset,
)


class TestEnqueue:
    @respx.mock
    async def test_enqueues_change(self, mock_ctx):
        respx.post(
            "https://zuul.example.com/api/tenant/test-tenant/project/org%2Frepo/enqueue"
        ).mock(return_value=httpx.Response(200, json={}))
        result = json.loads(
            await enqueue(mock_ctx, project="org/repo", pipeline="check", change="12345,1")
        )
        assert result["status"] == "enqueued"
        assert result["pipeline"] == "check"

    @respx.mock
    async def test_enqueues_ref(self, mock_ctx):
        respx.post(
            "https://zuul.example.com/api/tenant/test-tenant/project/org%2Frepo/enqueue"
        ).mock(return_value=httpx.Response(200, json={}))
        result = json.loads(
            await enqueue(mock_ctx, project="org/repo", pipeline="gate", ref="refs/heads/main")
        )
        assert result["status"] == "enqueued"

    async def test_requires_change_or_ref(self, mock_ctx):
        result = json.loads(await enqueue(mock_ctx, project="org/repo", pipeline="check"))
        assert "error" in result

    async def test_blocked_in_read_only(self, mock_ctx):
        mock_ctx.request_context.lifespan_context.config.read_only = True
        result = json.loads(
            await enqueue(mock_ctx, project="org/repo", pipeline="check", change="12345,1")
        )
        assert "error" in result
        assert "Write operations disabled" in result["error"]


class TestDequeue:
    @respx.mock
    async def test_dequeues_change(self, mock_ctx):
        respx.post(
            "https://zuul.example.com/api/tenant/test-tenant/project/org%2Frepo/dequeue"
        ).mock(return_value=httpx.Response(200, json={}))
        result = json.loads(
            await dequeue(mock_ctx, project="org/repo", pipeline="check", change="12345,1")
        )
        assert result["status"] == "dequeued"

    async def test_requires_change_or_ref(self, mock_ctx):
        result = json.loads(await dequeue(mock_ctx, project="org/repo", pipeline="check"))
        assert "error" in result


class TestAutoholdCreate:
    @respx.mock
    async def test_creates_autohold(self, mock_ctx):
        respx.post(
            "https://zuul.example.com/api/tenant/test-tenant/project/org%2Frepo/autohold"
        ).mock(return_value=httpx.Response(200, json={"id": "42"}))
        result = json.loads(
            await autohold_create(
                mock_ctx, project="org/repo", job="deploy-job", reason="debug infra"
            )
        )
        assert result["status"] == "created"
        assert result["job"] == "deploy-job"
        assert result["id"] == "42"


class TestAutoholdDelete:
    @respx.mock
    async def test_deletes_autohold(self, mock_ctx):
        respx.delete("https://zuul.example.com/api/tenant/test-tenant/autohold/42").mock(
            return_value=httpx.Response(204)
        )
        result = json.loads(await autohold_delete(mock_ctx, autohold_id="42"))
        assert result["status"] == "deleted"
        assert result["autohold_id"] == "42"


class TestEnqueueRef:
    @respx.mock
    async def test_enqueues_ref(self, mock_ctx):
        route = respx.post(
            "https://zuul.example.com/api/tenant/test-tenant"
            "/project/ci-framework%2Fintegration/enqueue"
        ).mock(return_value=httpx.Response(200, json={}))
        result = json.loads(
            await enqueue_ref(
                mock_ctx,
                project="ci-framework/integration",
                pipeline="periodic-pipeline",
                ref="refs/heads/shiftstack",
            )
        )
        assert result["status"] == "enqueued"
        assert result["pipeline"] == "periodic-pipeline"
        assert result["ref"] == "refs/heads/shiftstack"
        assert result["project"] == "ci-framework/integration"
        body = json.loads(route.calls[0].request.content)
        assert body == {
            "pipeline": "periodic-pipeline",
            "ref": "refs/heads/shiftstack",
            "oldrev": "",
            "newrev": "",
        }

    @respx.mock
    async def test_sends_oldrev_newrev(self, mock_ctx):
        route = respx.post(
            "https://zuul.example.com/api/tenant/test-tenant/project/org%2Frepo/enqueue"
        ).mock(return_value=httpx.Response(200, json={}))
        await enqueue_ref(
            mock_ctx,
            project="org/repo",
            pipeline="gate",
            ref="refs/heads/main",
            oldrev="abc123",
            newrev="def456",
        )
        body = json.loads(route.calls[0].request.content)
        assert body["oldrev"] == "abc123"
        assert body["newrev"] == "def456"
        assert body["ref"] == "refs/heads/main"

    async def test_blocked_in_read_only(self, mock_ctx):
        mock_ctx.request_context.lifespan_context.config.read_only = True
        result = json.loads(
            await enqueue_ref(
                mock_ctx,
                project="org/repo",
                pipeline="check",
                ref="refs/heads/main",
            )
        )
        assert "error" in result
        assert "Write operations disabled" in result["error"]


class TestReenqueueBuildset:
    @respx.mock
    async def test_reenqueues_from_buildset(self, mock_ctx):
        respx.get("https://zuul.example.com/api/tenant/test-tenant/buildset/bs-uuid-1").mock(
            return_value=httpx.Response(
                200,
                json={
                    "uuid": "bs-uuid-1",
                    "pipeline": "periodic-pipeline",
                    "refs": [
                        {
                            "project": "ci-framework/integration",
                            "ref": "refs/heads/shiftstack",
                            "change": None,
                        }
                    ],
                    "builds": [],
                },
            )
        )
        post_route = respx.post(
            "https://zuul.example.com/api/tenant/test-tenant"
            "/project/ci-framework%2Fintegration/enqueue"
        ).mock(return_value=httpx.Response(200, json={}))
        result = json.loads(await reenqueue_buildset(mock_ctx, uuid="bs-uuid-1"))
        assert result["status"] == "enqueued"
        assert result["project"] == "ci-framework/integration"
        assert result["pipeline"] == "periodic-pipeline"
        assert result["ref"] == "refs/heads/shiftstack"
        assert result["from_buildset"] == "bs-uuid-1"
        body = json.loads(post_route.calls[0].request.content)
        assert body == {
            "pipeline": "periodic-pipeline",
            "ref": "refs/heads/shiftstack",
            "oldrev": "",
            "newrev": "",
        }

    @respx.mock
    async def test_reenqueues_via_url(self, mock_ctx):
        respx.get("https://zuul.example.com/api/tenant/test-tenant/buildset/bs-url-1").mock(
            return_value=httpx.Response(
                200,
                json={
                    "uuid": "bs-url-1",
                    "pipeline": "gate",
                    "refs": [{"project": "org/repo", "ref": "refs/heads/main"}],
                    "builds": [],
                },
            )
        )
        respx.post(
            "https://zuul.example.com/api/tenant/test-tenant/project/org%2Frepo/enqueue"
        ).mock(return_value=httpx.Response(200, json={}))
        result = json.loads(
            await reenqueue_buildset(
                mock_ctx,
                url="https://zuul.example.com/t/test-tenant/buildset/bs-url-1",
            )
        )
        assert result["status"] == "enqueued"
        assert result["from_buildset"] == "bs-url-1"

    @respx.mock
    async def test_errors_on_no_ref(self, mock_ctx):
        respx.get("https://zuul.example.com/api/tenant/test-tenant/buildset/bs-no-ref").mock(
            return_value=httpx.Response(
                200,
                json={
                    "uuid": "bs-no-ref",
                    "pipeline": "check",
                    "refs": [{"project": "org/repo", "change": 12345}],
                    "builds": [],
                },
            )
        )
        result = json.loads(await reenqueue_buildset(mock_ctx, uuid="bs-no-ref"))
        assert "error" in result
        assert "no ref" in result["error"]

    @respx.mock
    async def test_errors_on_no_refs(self, mock_ctx):
        respx.get("https://zuul.example.com/api/tenant/test-tenant/buildset/bs-empty").mock(
            return_value=httpx.Response(
                200,
                json={
                    "uuid": "bs-empty",
                    "pipeline": "check",
                    "refs": [],
                    "builds": [],
                },
            )
        )
        result = json.loads(await reenqueue_buildset(mock_ctx, uuid="bs-empty"))
        assert "error" in result
        assert "no refs" in result["error"]

    async def test_blocked_in_read_only(self, mock_ctx):
        mock_ctx.request_context.lifespan_context.config.read_only = True
        result = json.loads(await reenqueue_buildset(mock_ctx, uuid="bs-uuid-1"))
        assert "error" in result
        assert "Write operations disabled" in result["error"]

    async def test_requires_uuid_or_url(self, mock_ctx):
        result = json.loads(await reenqueue_buildset(mock_ctx))
        assert "error" in result

    @respx.mock
    async def test_errors_on_no_project_in_ref(self, mock_ctx):
        respx.get("https://zuul.example.com/api/tenant/test-tenant/buildset/bs-no-proj").mock(
            return_value=httpx.Response(
                200,
                json={
                    "uuid": "bs-no-proj",
                    "pipeline": "check",
                    "refs": [{"ref": "refs/heads/main"}],
                    "builds": [],
                },
            )
        )
        result = json.loads(await reenqueue_buildset(mock_ctx, uuid="bs-no-proj"))
        assert "error" in result
        assert "no project" in result["error"]

    @respx.mock
    async def test_errors_on_no_pipeline(self, mock_ctx):
        respx.get("https://zuul.example.com/api/tenant/test-tenant/buildset/bs-no-pipe").mock(
            return_value=httpx.Response(
                200,
                json={
                    "uuid": "bs-no-pipe",
                    "refs": [{"project": "org/repo", "ref": "refs/heads/main"}],
                    "builds": [],
                },
            )
        )
        result = json.loads(await reenqueue_buildset(mock_ctx, uuid="bs-no-pipe"))
        assert "error" in result
        assert "no pipeline" in result["error"]

    @respx.mock
    async def test_handles_api_error(self, mock_ctx):
        respx.get("https://zuul.example.com/api/tenant/test-tenant/buildset/bs-404").mock(
            return_value=httpx.Response(404, text="Not Found")
        )
        result = json.loads(await reenqueue_buildset(mock_ctx, uuid="bs-404"))
        assert "error" in result

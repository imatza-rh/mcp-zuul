"""Tests for write operations (enqueue, dequeue, autohold)."""

import json

import httpx
import respx

from mcp_zuul.tools import autohold_create, autohold_delete, dequeue, enqueue


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

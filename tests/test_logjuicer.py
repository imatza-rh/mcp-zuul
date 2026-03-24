"""Tests for LogJuicer integration."""

import json

import httpx
import respx

from mcp_zuul.tools import get_build_anomalies
from tests.conftest import make_build


class TestGetBuildAnomalies:
    async def test_not_configured(self, mock_ctx):
        mock_ctx.request_context.lifespan_context.config.logjuicer_url = None
        result = json.loads(await get_build_anomalies(mock_ctx, uuid="u1"))
        assert "error" in result
        assert "not configured" in result["error"]

    @respx.mock
    async def test_returns_anomalies(self, mock_ctx):
        mock_ctx.request_context.lifespan_context.config.logjuicer_url = (
            "https://logjuicer.example.com"
        )
        build = make_build(uuid="lj-uuid", result="FAILURE")
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/lj-uuid").mock(
            return_value=httpx.Response(200, json=build)
        )
        respx.put("https://logjuicer.example.com/api/report/new").mock(
            return_value=httpx.Response(200, json={"id": 99})
        )
        respx.get("https://logjuicer.example.com/api/report/99/json").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {
                        "anomalies": [
                            {
                                "line": "FATAL: connection refused",
                                "pos": 42,
                                "before": "connecting...",
                                "after": "retrying...",
                            },
                            {
                                "line": "ERROR: timeout",
                                "pos": 55,
                                "before": "waiting...",
                                "after": None,
                            },
                        ]
                    }
                ],
            )
        )
        result = json.loads(await get_build_anomalies(mock_ctx, uuid="lj-uuid"))
        assert result["anomaly_count"] == 2
        assert result["report_id"] == "99"
        assert result["anomalies"][0]["line"] == "FATAL: connection refused"
        assert result["job"] == "test-job"

    @respx.mock
    async def test_no_log_url_completed(self, mock_ctx):
        """Completed build with no log_url should mention lost/aborted logs."""
        mock_ctx.request_context.lifespan_context.config.logjuicer_url = (
            "https://logjuicer.example.com"
        )
        build = make_build(uuid="no-log", log_url=None)
        build["log_url"] = None
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/no-log").mock(
            return_value=httpx.Response(200, json=build)
        )
        result = json.loads(await get_build_anomalies(mock_ctx, uuid="no-log"))
        assert "error" in result
        assert "lost" in result["error"] or "aborted" in result["error"]

    @respx.mock
    async def test_no_log_url_in_progress(self, mock_ctx):
        """In-progress build should return status-aware error."""
        mock_ctx.request_context.lifespan_context.config.logjuicer_url = (
            "https://logjuicer.example.com"
        )
        build = make_build(uuid="in-prog", log_url=None)
        build["log_url"] = None
        build["result"] = None
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/in-prog").mock(
            return_value=httpx.Response(200, json=build)
        )
        result = json.loads(await get_build_anomalies(mock_ctx, uuid="in-prog"))
        assert "error" in result
        assert "in progress" in result["error"]

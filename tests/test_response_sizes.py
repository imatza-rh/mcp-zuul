"""Response size regression tests.

Ensures tool responses stay token-efficient by asserting byte-size limits.
If a change causes a response to exceed its threshold, investigate whether
the extra data is necessary or if the formatter needs updating.

Thresholds are set to ~2x the measured mock response size. This catches
accidental bloat while allowing reasonable field growth.
"""

import json

import httpx
import respx

from mcp_zuul.tools import (
    get_build,
    get_build_failures,
    get_buildset,
    get_status,
    list_builds,
    list_buildsets,
)
from tests.conftest import (
    make_build,
    make_buildset,
    make_job_output_json,
    make_status_pipeline,
)

# Measured mock response sizes (bytes):
#   list_builds(20, brief):  ~2,700
#   get_build(full):           ~490
#   get_buildset(full):        ~500
#   list_buildsets(10, brief): ~1,400
#   get_status(5 items):     ~2,800
#   get_build_failures:        ~500
#
# Thresholds are ~2x measured to allow growth without masking bloat.

KB = 1024


class TestResponseSizes:
    """Assert tool responses stay within token-efficient size limits."""

    @respx.mock
    async def test_list_builds_20_under_limit(self, mock_ctx):
        """20 builds (brief) should stay compact."""
        builds = [make_build(uuid=f"uuid-{i}", job_name=f"job-{i}") for i in range(20)]
        respx.get("https://zuul.example.com/api/tenant/test-tenant/builds").mock(
            return_value=httpx.Response(200, json=builds)
        )
        result = await list_builds(mock_ctx, limit=20)
        size = len(result.encode())
        assert size < 6 * KB, f"list_builds(20) bloat: {size} bytes (limit: {6 * KB})"

    @respx.mock
    async def test_get_build_under_limit(self, mock_ctx):
        """Single build (full detail) should be small."""
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/build-uuid-1").mock(
            return_value=httpx.Response(200, json=make_build())
        )
        result = await get_build(mock_ctx, uuid="build-uuid-1")
        size = len(result.encode())
        assert size < 1 * KB, f"get_build bloat: {size} bytes (limit: {1 * KB})"

    @respx.mock
    async def test_get_buildset_under_limit(self, mock_ctx):
        """Buildset with 1 build (full detail) should be compact."""
        respx.get("https://zuul.example.com/api/tenant/test-tenant/buildset/bs-uuid-1").mock(
            return_value=httpx.Response(200, json=make_buildset())
        )
        result = await get_buildset(mock_ctx, uuid="bs-uuid-1", brief=False)
        size = len(result.encode())
        assert size < int(1.5 * KB), f"get_buildset bloat: {size} bytes (limit: {int(1.5 * KB)})"

    @respx.mock
    async def test_list_buildsets_10_under_limit(self, mock_ctx):
        """10 buildsets (brief) should stay compact."""
        buildsets = [make_buildset(uuid=f"bs-{i}") for i in range(10)]
        respx.get("https://zuul.example.com/api/tenant/test-tenant/buildsets").mock(
            return_value=httpx.Response(200, json=buildsets)
        )
        result = await list_buildsets(mock_ctx, limit=10)
        size = len(result.encode())
        assert size < 3 * KB, f"list_buildsets(10) bloat: {size} bytes (limit: {3 * KB})"

    @respx.mock
    async def test_get_status_5_items_under_limit(self, mock_ctx):
        """Status with 5 pipeline items should stay compact."""
        pipelines = [make_status_pipeline(name=f"pipe-{i}") for i in range(5)]
        respx.get("https://zuul.example.com/api/tenant/test-tenant/status").mock(
            return_value=httpx.Response(200, json={"pipelines": pipelines})
        )
        result = await get_status(mock_ctx)
        size = len(result.encode())
        assert size < 6 * KB, f"get_status(5 items) bloat: {size} bytes (limit: {6 * KB})"

    @respx.mock
    async def test_get_build_failures_under_limit(self, mock_ctx):
        """Build failures (structured) should be compact."""
        build = make_build(result="FAILURE")
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/fail-uuid").mock(
            return_value=httpx.Response(200, json=build)
        )
        # get_build_failures tries .gz first, then falls back to .json
        respx.get(f"{build['log_url']}job-output.json.gz").mock(
            return_value=httpx.Response(200, json=make_job_output_json(failed=True))
        )
        result = await get_build_failures(mock_ctx, uuid="fail-uuid")
        parsed = json.loads(result)
        assert "failed_tasks" in parsed, f"Expected failure data, got: {list(parsed.keys())}"
        size = len(result.encode())
        assert size < int(1.5 * KB), (
            f"get_build_failures bloat: {size} bytes (limit: {int(1.5 * KB)})"
        )

    @respx.mock
    async def test_clean_strips_none_at_all_levels(self, mock_ctx):
        """Verify clean() is applied — no null values in responses."""
        build = make_build()
        build["error_detail"] = None
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/build-uuid-1").mock(
            return_value=httpx.Response(200, json=build)
        )
        result = await get_build(mock_ctx, uuid="build-uuid-1")
        raw = result.encode()
        # No "null" should appear in the JSON output (clean() strips all Nones)
        assert b": null" not in raw and b":null" not in raw, (
            f"Found null values in response — clean() not applied: {result}"
        )

    @respx.mock
    async def test_brief_smaller_than_full(self, mock_ctx):
        """Brief mode should produce strictly smaller output than full."""
        from mcp_zuul.formatters import fmt_build

        build = make_build()
        brief_size = len(json.dumps(fmt_build(build, brief=True)).encode())
        full_size = len(json.dumps(fmt_build(build, brief=False)).encode())
        assert brief_size < full_size, (
            f"Brief ({brief_size}B) should be smaller than full ({full_size}B)"
        )
        # Brief should be at least 20% smaller
        savings_pct = (1 - brief_size / full_size) * 100
        assert savings_pct > 20, f"Brief saves only {savings_pct:.0f}% — should save >20%"


class TestTokenOptimizations:
    """Tests for v0.10.0 token optimization features."""

    @respx.mock
    async def test_diagnose_build_brief(self, mock_ctx):
        """diagnose_build(brief=True) returns compact classification only."""
        from mcp_zuul.tools import diagnose_build

        build = make_build(result="FAILURE")
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/fail-uuid").mock(
            return_value=httpx.Response(200, json=build)
        )
        respx.get(f"{build['log_url']}job-output.json.gz").mock(
            return_value=httpx.Response(200, json=make_job_output_json(failed=True))
        )
        respx.get(f"{build['log_url']}job-output.txt").mock(
            return_value=httpx.Response(200, text="line1\nline2\n")
        )
        # Full response
        full_result = await diagnose_build(mock_ctx, uuid="fail-uuid")
        full = json.loads(full_result)
        # Brief response
        brief_result = await diagnose_build(mock_ctx, uuid="fail-uuid", brief=True)
        brief = json.loads(brief_result)

        # Brief must be strictly smaller
        assert len(brief_result) < len(full_result), "Brief should be smaller than full"
        # Brief must include classification
        assert "classification" in brief or "result" in brief
        # Brief must NOT include verbose fields
        assert "playbooks" not in brief
        assert "log_context" not in brief
        assert "files_in_failure" not in brief
        # log_url IS included in brief (enables direct_log_url pass-through)
        # Brief must use SAME key names as full for classification fields
        if "classification" in brief:
            assert "classification_confidence" in brief, (
                "Brief uses 'confidence' but full uses 'classification_confidence' — keys must match"
            )
        # Brief should include root cause if tasks exist
        if full.get("failed_tasks"):
            assert "root_cause" in brief

    @respx.mock
    async def test_find_flaky_jobs_no_detail(self, mock_ctx):
        """find_flaky_jobs(detail=False) omits builds array."""
        from mcp_zuul.tools import find_flaky_jobs

        builds = [
            {"uuid": f"u{i}", "result": "SUCCESS" if i % 2 else "FAILURE", "duration": 100}
            for i in range(10)
        ]
        respx.get("https://zuul.example.com/api/tenant/test-tenant/builds").mock(
            return_value=httpx.Response(200, json=builds)
        )
        result = json.loads(await find_flaky_jobs(mock_ctx, job_name="j"))
        assert "builds" not in result
        assert "failure_rate" in result

        # With detail=True, builds are included
        result_detail = json.loads(await find_flaky_jobs(mock_ctx, job_name="j", detail=True))
        assert "builds" in result_detail
        assert len(result_detail["builds"]) == 10

    @respx.mock
    async def test_get_build_times_no_detail(self, mock_ctx):
        """get_build_times(detail=False) omits builds array."""
        from mcp_zuul.tools import get_build_times

        builds = [
            {"uuid": f"u{i}", "job_name": "j", "result": "SUCCESS", "duration": 100 + i}
            for i in range(5)
        ]
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build-times").mock(
            return_value=httpx.Response(200, json=builds)
        )
        result = json.loads(await get_build_times(mock_ctx, detail=False))
        assert "builds" not in result
        assert "stats" in result
        assert result["count"] == 5

    @respx.mock
    async def test_get_freeze_job_no_vars(self, mock_ctx):
        """get_freeze_job excludes vars by default."""
        from mcp_zuul.tools import get_freeze_job

        respx.get(
            "https://zuul.example.com/api/tenant/test-tenant"
            "/pipeline/check/project/org%2Frepo/branch/main/freeze-job/j"
        ).mock(
            return_value=httpx.Response(
                200,
                json={
                    "job": "j",
                    "timeout": 3600,
                    "nodeset": {"name": "n", "nodes": [{"name": "c", "label": "l"}]},
                    "playbooks": [{"project": "p", "path": "run.yml", "trusted": False}],
                    "vars": {"big_var": "x" * 1000},
                    "extra_vars": {"more": "data"},
                    "host_vars": {},
                    "group_vars": {},
                },
            )
        )
        result = json.loads(
            await get_freeze_job(mock_ctx, pipeline="check", project="org/repo", job_name="j")
        )
        assert "vars" not in result
        assert "extra_vars" not in result
        assert result["job"] == "j"

        # With include_vars=True
        result_v = json.loads(
            await get_freeze_job(
                mock_ctx, pipeline="check", project="org/repo", job_name="j", include_vars=True
            )
        )
        assert result_v["vars"] == {"big_var": "x" * 1000}

    @respx.mock
    async def test_tail_build_log_direct_log_url(self, mock_ctx):
        """tail_build_log with direct_log_url skips build API call."""
        from mcp_zuul.tools import tail_build_log

        log_url = "https://logs.example.com/build-1/"
        log_text = "\n".join(f"line {i}" for i in range(100))
        respx.get(f"{log_url}job-output.txt").mock(return_value=httpx.Response(200, text=log_text))
        # No build API mock — if it tries to call the build API, it will fail
        result = json.loads(await tail_build_log(mock_ctx, direct_log_url=log_url, lines=10))
        assert result["count"] == 10
        assert "lines" in result
        # Empty job/result should be cleaned from response
        assert "job" not in result or result.get("job")
        assert "result" not in result or result.get("result")

    @respx.mock
    async def test_direct_log_url_rejects_bad_scheme(self, mock_ctx):
        """direct_log_url must reject non-http(s) schemes (SSRF prevention)."""
        from mcp_zuul.tools import get_build_log, tail_build_log

        for tool in (get_build_log, tail_build_log):
            result = json.loads(await tool(mock_ctx, direct_log_url="file:///etc/passwd"))
            assert "error" in result
            result = json.loads(await tool(mock_ctx, direct_log_url="ftp://internal/data"))
            assert "error" in result

    @respx.mock
    async def test_direct_log_url_rejects_no_hostname(self, mock_ctx):
        """direct_log_url must reject URLs without a hostname."""
        from mcp_zuul.tools import get_build_log

        result = json.loads(await get_build_log(mock_ctx, direct_log_url="https://"))
        assert "error" in result

    async def test_docstring_total_chars(self):
        """Guard against docstring re-bloat."""
        import ast
        import os

        total = 0
        for root, _dirs, files in os.walk("src/mcp_zuul/tools"):
            for fname in files:
                if not fname.endswith(".py"):
                    continue
                fpath = os.path.join(root, fname)
                with open(fpath) as f:
                    src = f.read()
                tree = ast.parse(src)
                for node in ast.walk(tree):
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        ds = ast.get_docstring(node)
                        if ds and not node.name.startswith("_"):
                            total += len(ds)
        assert total < 15000, f"Docstring total {total} chars exceeds 15K budget"

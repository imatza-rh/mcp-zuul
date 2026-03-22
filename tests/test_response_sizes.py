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
#   list_builds(20, brief):  ~6,600
#   get_build(full):           ~510
#   get_buildset(full):        ~700
#   list_buildsets(10, brief): ~2,000
#   get_status(5 items):     ~3,800
#   get_build_failures:        ~700
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
        assert size < 12 * KB, f"list_builds(20) bloat: {size} bytes (limit: {12 * KB})"

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
        result = await get_buildset(mock_ctx, uuid="bs-uuid-1")
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
        assert size < 4 * KB, f"list_buildsets(10) bloat: {size} bytes (limit: {4 * KB})"

    @respx.mock
    async def test_get_status_5_items_under_limit(self, mock_ctx):
        """Status with 5 pipeline items should stay compact."""
        pipelines = [make_status_pipeline(name=f"pipe-{i}") for i in range(5)]
        respx.get("https://zuul.example.com/api/tenant/test-tenant/status").mock(
            return_value=httpx.Response(200, json={"pipelines": pipelines})
        )
        result = await get_status(mock_ctx)
        size = len(result.encode())
        assert size < 8 * KB, f"get_status(5 items) bloat: {size} bytes (limit: {8 * KB})"

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

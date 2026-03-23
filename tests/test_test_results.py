"""Tests for test results parsing (get_build_test_results)."""

import json

import httpx
import respx

from mcp_zuul.tools import get_build_test_results
from tests.conftest import make_build


def _junit_xml(
    tests: int = 3,
    failures: int = 0,
    errors: int = 0,
    skipped: int = 0,
    time: float = 100.0,
) -> str:
    """Generate a JUnit XML string for testing."""
    cases = []
    for i in range(skipped):
        cases.append(
            f'<testcase name="test_skip_{i}" time="0.0"><skipped>reason</skipped></testcase>'
        )
    for i in range(failures):
        cases.append(
            f'<testcase classname="tests.TestFoo" name="test_fail_{i}" time="1.5">'
            f'<failure message="assert False" type="AssertionError">traceback</failure>'
            f"</testcase>"
        )
    for i in range(errors):
        cases.append(
            f'<testcase classname="tests.TestFoo" name="test_err_{i}" time="0.5">'
            f'<error message="RuntimeError" type="RuntimeError">traceback</error>'
            f"</testcase>"
        )
    passed = tests - failures - errors - skipped
    for i in range(max(0, passed)):
        cases.append(f'<testcase classname="tests.TestFoo" name="test_pass_{i}" time="2.0"/>')
    return (
        f'<testsuite name="tempest" tests="{tests}" failures="{failures}" '
        f'errors="{errors}" time="{time}">' + "\n".join(cases) + "</testsuite>"
    )


def _manifest_with_xml(
    path: str = "controller/ci-framework-data/tests/test_operator/tempest-tests-tempest/tempest_results.xml",
) -> dict:
    """Create a zuul-manifest.json structure with a test XML."""
    parts = path.split("/")
    tree: list = []
    current = tree
    for i, part in enumerate(parts):
        node: dict = {"name": part}
        if i < len(parts) - 1:
            node["children"] = []
            current.append(node)
            current = node["children"]
        else:
            current.append(node)
    return {"tree": tree}


class TestGetBuildTestResults:
    @respx.mock
    async def test_parses_successful_results(self, mock_ctx):
        build = make_build(uuid="test-uuid", result="SUCCESS")
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/test-uuid").mock(
            return_value=httpx.Response(200, json=build)
        )
        respx.get(f"{build['log_url']}zuul-manifest.json").mock(
            return_value=httpx.Response(
                200,
                json=_manifest_with_xml(),
            )
        )
        respx.get(
            f"{build['log_url']}controller/ci-framework-data/tests/test_operator/"
            "tempest-tests-tempest/tempest_results.xml"
        ).mock(
            return_value=httpx.Response(
                200, text=_junit_xml(tests=10, failures=0, skipped=3, time=500.0)
            )
        )
        result = json.loads(await get_build_test_results(mock_ctx, uuid="test-uuid"))
        assert result["job"] == "test-job"
        assert result["suite_count"] == 1
        assert result["totals"]["tests"] == 10
        assert result["totals"]["passed"] == 7
        assert result["totals"]["skipped"] == 3
        assert result["totals"]["failed"] == 0

    @respx.mock
    async def test_parses_failed_tests(self, mock_ctx):
        build = make_build(uuid="fail-uuid", result="FAILURE")
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/fail-uuid").mock(
            return_value=httpx.Response(200, json=build)
        )
        respx.get(f"{build['log_url']}zuul-manifest.json").mock(
            return_value=httpx.Response(200, json=_manifest_with_xml())
        )
        respx.get(
            f"{build['log_url']}controller/ci-framework-data/tests/test_operator/"
            "tempest-tests-tempest/tempest_results.xml"
        ).mock(
            return_value=httpx.Response(
                200, text=_junit_xml(tests=5, failures=2, errors=1, skipped=0)
            )
        )
        result = json.loads(await get_build_test_results(mock_ctx, uuid="fail-uuid"))
        assert result["totals"]["failed"] == 2
        assert result["totals"]["errored"] == 1
        assert result["totals"]["passed"] == 2
        suite = result["test_suites"][0]
        assert len(suite["failures"]) == 3  # 2 failures + 1 error
        assert suite["failures"][0]["name"] == "test_fail_0"
        assert suite["failures"][0]["message"] == "assert False"

    @respx.mock
    async def test_no_manifest_falls_back(self, mock_ctx):
        build = make_build(uuid="fb-uuid")
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/fb-uuid").mock(
            return_value=httpx.Response(200, json=build)
        )
        respx.get(f"{build['log_url']}zuul-manifest.json").mock(return_value=httpx.Response(404))
        # Fallback path exists
        respx.get(
            f"{build['log_url']}controller/ci-framework-data/tests/test_operator/"
            "tempest-tests-tempest/tempest_results.xml"
        ).mock(return_value=httpx.Response(200, text=_junit_xml(tests=3, failures=0)))
        # Tobiko fallback doesn't exist
        respx.get(
            f"{build['log_url']}controller/ci-framework-data/tests/test_operator/"
            "tobiko-tests-tobiko/tobiko_results.xml"
        ).mock(return_value=httpx.Response(404))
        result = json.loads(await get_build_test_results(mock_ctx, uuid="fb-uuid"))
        assert result["suite_count"] == 1
        assert result["totals"]["tests"] == 3

    @respx.mock
    async def test_no_results_found(self, mock_ctx):
        build = make_build(uuid="no-tests")
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/no-tests").mock(
            return_value=httpx.Response(200, json=build)
        )
        respx.get(f"{build['log_url']}zuul-manifest.json").mock(
            return_value=httpx.Response(200, json={"tree": []})
        )
        # Fallback paths don't exist
        respx.get(
            f"{build['log_url']}controller/ci-framework-data/tests/test_operator/"
            "tempest-tests-tempest/tempest_results.xml"
        ).mock(return_value=httpx.Response(404))
        respx.get(
            f"{build['log_url']}controller/ci-framework-data/tests/test_operator/"
            "tobiko-tests-tobiko/tobiko_results.xml"
        ).mock(return_value=httpx.Response(404))
        result = json.loads(await get_build_test_results(mock_ctx, uuid="no-tests"))
        assert "error" in result
        assert "No test results" in result["error"]

    @respx.mock
    async def test_accepts_url(self, mock_ctx):
        build = make_build(uuid="url-uuid")
        respx.get("https://zuul.example.com/api/tenant/my-tenant/build/url-uuid").mock(
            return_value=httpx.Response(200, json=build)
        )
        respx.get(f"{build['log_url']}zuul-manifest.json").mock(
            return_value=httpx.Response(200, json=_manifest_with_xml())
        )
        respx.get(
            f"{build['log_url']}controller/ci-framework-data/tests/test_operator/"
            "tempest-tests-tempest/tempest_results.xml"
        ).mock(return_value=httpx.Response(200, text=_junit_xml(tests=1)))
        result = json.loads(
            await get_build_test_results(
                mock_ctx, url="https://zuul.example.com/t/my-tenant/build/url-uuid"
            )
        )
        assert result["totals"]["tests"] == 1

    @respx.mock
    async def test_skips_non_junit_xml(self, mock_ctx):
        build = make_build(uuid="non-junit")
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/non-junit").mock(
            return_value=httpx.Response(200, json=build)
        )
        manifest = {
            "tree": [
                {
                    "name": "controller",
                    "children": [
                        {
                            "name": "ci-framework-data",
                            "children": [
                                {
                                    "name": "tests",
                                    "children": [{"name": "config.xml"}],
                                }
                            ],
                        }
                    ],
                }
            ]
        }
        respx.get(f"{build['log_url']}zuul-manifest.json").mock(
            return_value=httpx.Response(200, json=manifest)
        )
        respx.get(f"{build['log_url']}controller/ci-framework-data/tests/config.xml").mock(
            return_value=httpx.Response(200, text="<config><setting>value</setting></config>")
        )
        # Fallback paths
        respx.get(
            f"{build['log_url']}controller/ci-framework-data/tests/test_operator/"
            "tempest-tests-tempest/tempest_results.xml"
        ).mock(return_value=httpx.Response(404))
        respx.get(
            f"{build['log_url']}controller/ci-framework-data/tests/test_operator/"
            "tobiko-tests-tobiko/tobiko_results.xml"
        ).mock(return_value=httpx.Response(404))
        result = json.loads(await get_build_test_results(mock_ctx, uuid="non-junit"))
        assert "error" in result
        assert "none contained valid" in result["error"]

    async def test_no_log_url_completed(self, mock_ctx):
        """Completed build with no log_url should mention lost/aborted logs."""
        build = make_build(uuid="no-log", log_url=None)
        build["log_url"] = None
        respx.mock(assert_all_mocked=False)
        with respx.mock:
            respx.get("https://zuul.example.com/api/tenant/test-tenant/build/no-log").mock(
                return_value=httpx.Response(200, json=build)
            )
            result = json.loads(await get_build_test_results(mock_ctx, uuid="no-log"))
        assert "error" in result
        assert "lost" in result["error"] or "aborted" in result["error"]

    async def test_no_log_url_in_progress(self, mock_ctx):
        """In-progress build should return status-aware error."""
        build = make_build(uuid="in-prog", log_url=None)
        build["log_url"] = None
        build["result"] = None
        with respx.mock:
            respx.get("https://zuul.example.com/api/tenant/test-tenant/build/in-prog").mock(
                return_value=httpx.Response(200, json=build)
            )
            result = json.loads(await get_build_test_results(mock_ctx, uuid="in-prog"))
        assert "error" in result
        assert "in progress" in result["error"]

    @respx.mock
    async def test_testsuites_wrapper(self, mock_ctx):
        """Handle <testsuites> wrapper around multiple <testsuite> elements."""
        build = make_build(uuid="multi-uuid")
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/multi-uuid").mock(
            return_value=httpx.Response(200, json=build)
        )
        respx.get(f"{build['log_url']}zuul-manifest.json").mock(
            return_value=httpx.Response(200, json=_manifest_with_xml())
        )
        xml = (
            "<testsuites>"
            '<testsuite name="suite1" tests="2" time="10">'
            '<testcase name="test_a" time="5"/>'
            '<testcase name="test_b" time="5"/>'
            "</testsuite>"
            '<testsuite name="suite2" tests="1" time="3">'
            '<testcase name="test_c" time="3"/>'
            "</testsuite>"
            "</testsuites>"
        )
        respx.get(
            f"{build['log_url']}controller/ci-framework-data/tests/test_operator/"
            "tempest-tests-tempest/tempest_results.xml"
        ).mock(return_value=httpx.Response(200, text=xml))
        result = json.loads(await get_build_test_results(mock_ctx, uuid="multi-uuid"))
        assert result["totals"]["tests"] == 3
        assert result["totals"]["passed"] == 3

    @respx.mock
    async def test_empty_time_attribute(self, mock_ctx):
        """Handle testcases with empty or missing time attributes."""
        build = make_build(uuid="empty-time")
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/empty-time").mock(
            return_value=httpx.Response(200, json=build)
        )
        respx.get(f"{build['log_url']}zuul-manifest.json").mock(
            return_value=httpx.Response(200, json=_manifest_with_xml())
        )
        xml = (
            '<testsuite tests="2" time="">'
            '<testcase name="test_a" time=""><failure message="bad"/></testcase>'
            '<testcase name="test_b"/>'
            "</testsuite>"
        )
        respx.get(
            f"{build['log_url']}controller/ci-framework-data/tests/test_operator/"
            "tempest-tests-tempest/tempest_results.xml"
        ).mock(return_value=httpx.Response(200, text=xml))
        result = json.loads(await get_build_test_results(mock_ctx, uuid="empty-time"))
        assert result["totals"]["failed"] == 1
        assert result["totals"]["passed"] == 1
        assert result["test_suites"][0]["failures"][0]["time"] == 0.0

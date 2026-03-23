"""JUnit XML test results parsing tools."""

import contextlib
import json
from typing import Any

import defusedxml.ElementTree as ET
from mcp.server.fastmcp import Context

from ..errors import handle_errors
from ..helpers import api, app, clean, error, fetch_log_url, safepath
from ..server import mcp
from ._common import _READ_ONLY, _no_log_url_error, _resolve

_MAX_XML_BYTES = 5 * 1024 * 1024  # 5 MB per XML file


def _find_test_xmls(tree: list, path: str = "") -> list[str]:
    """Walk zuul-manifest.json tree to find JUnit XML test result files."""
    results = []
    for item in tree:
        name = item.get("name", "")
        full = f"{path}/{name}" if path else name
        if (
            name.endswith(".xml")
            and "test" in full.lower()
            and not name.endswith(".yaml")
            and "must-gather" not in full
            and "crd" not in full.lower()
        ):
            results.append(full)
        if "children" in item:
            results.extend(_find_test_xmls(item["children"], full))
    return results


def _safe_float(val: Any, default: float = 0.0) -> float:
    """Convert to float, returning default on failure."""
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def _parse_junit_xml(content: str, file_path: str) -> dict | None:
    """Parse a JUnit XML file and return structured test results."""
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return None

    # Must be a testsuite or testsuites element
    if root.tag not in ("testsuite", "testsuites"):
        return None

    # Handle testsuites wrapper
    suites = root.findall("testsuite") if root.tag == "testsuites" else [root]

    all_passed = 0
    all_skipped = 0
    all_failed = 0
    all_errored = 0
    all_failures: list[dict] = []
    total_time = 0.0

    for suite in suites:
        for tc in suite.findall("testcase"):
            skip_el = tc.find("skipped")
            fail_el = tc.find("failure")
            err_el = tc.find("error")
            if skip_el is not None:
                all_skipped += 1
            elif fail_el is not None:
                all_failed += 1
                all_failures.append(
                    clean(
                        {
                            "name": tc.get("name", "")[:200],
                            "classname": tc.get("classname") or None,
                            "time": _safe_float(tc.get("time", 0)),
                            "message": (fail_el.get("message") or "")[:500] or None,
                            "type": fail_el.get("type") or None,
                        }
                    )
                )
            elif err_el is not None:
                all_errored += 1
                all_failures.append(
                    clean(
                        {
                            "name": tc.get("name", "")[:200],
                            "classname": tc.get("classname") or None,
                            "time": _safe_float(tc.get("time", 0)),
                            "message": (err_el.get("message") or "")[:500] or None,
                            "type": err_el.get("type") or None,
                            "error": True,
                        }
                    )
                )
            else:
                all_passed += 1

        with contextlib.suppress(ValueError, TypeError):
            total_time += float(suite.get("time", 0))

    total = all_passed + all_skipped + all_failed + all_errored
    if total == 0:
        return None

    return clean(
        {
            "file": file_path,
            "name": suites[0].get("name") or None,
            "tests": total,
            "passed": all_passed,
            "skipped": all_skipped,
            "failed": all_failed,
            "errored": all_errored,
            "time": round(total_time, 2),
            "failures": all_failures or None,
        }
    )


@mcp.tool(title="Test Results", annotations=_READ_ONLY)
@handle_errors
async def get_build_test_results(
    ctx: Context,
    uuid: str = "",
    tenant: str = "",
    url: str = "",
) -> str:
    """Parse JUnit XML test results from a build's log directory.

    Discovers test result files via zuul-manifest.json and parses
    JUnit XML to return structured pass/fail/skip counts with
    failure details. Works with tempest, tobiko, and any test
    framework that produces JUnit XML output.

    Args:
        uuid: Build UUID
        tenant: Tenant name (uses default if empty)
        url: Zuul build URL (alternative to uuid + tenant)
    """
    uuid, t = _resolve(ctx, uuid, tenant, url, "build")
    build = await api(ctx, f"/tenant/{safepath(t)}/build/{safepath(uuid)}")
    log_url = build.get("log_url")
    if not log_url:
        return _no_log_url_error(build, uuid)

    a = app(ctx)
    base = log_url.rstrip("/")

    # Step 1: Discover test XML files via zuul-manifest.json
    manifest_resp = await fetch_log_url(a, f"{base}/zuul-manifest.json")
    xml_paths: list[str] = []
    if manifest_resp.status_code == 200:
        try:
            manifest = manifest_resp.json()
            xml_paths = _find_test_xmls(manifest.get("tree", []))
        except Exception:
            pass

    # Step 2: Fallback — try common paths if no manifest
    if not xml_paths:
        common_paths = [
            "controller/ci-framework-data/tests/test_operator/tempest-tests-tempest/tempest_results.xml",
            "controller/ci-framework-data/tests/test_operator/tobiko-tests-tobiko/tobiko_results.xml",
        ]
        for path in common_paths:
            resp = await fetch_log_url(a, f"{base}/{path}")
            if resp.status_code == 200:
                xml_paths.append(path)

    if not xml_paths:
        return error(
            "No test results found. Use browse_build_logs to check "
            "if tests ran and where results are stored."
        )

    # Step 3: Fetch and parse each XML file
    test_suites = []
    for xml_path in xml_paths[:10]:  # Cap at 10 files
        resp = await fetch_log_url(a, f"{base}/{xml_path}")
        if resp.status_code != 200:
            continue
        content = resp.content[:_MAX_XML_BYTES].decode("utf-8", errors="replace")
        parsed = _parse_junit_xml(content, xml_path)
        if parsed:
            test_suites.append(parsed)

    if not test_suites:
        return error("Found XML files but none contained valid JUnit test results.")

    # Step 4: Compute totals
    totals = {"tests": 0, "passed": 0, "skipped": 0, "failed": 0, "errored": 0}
    for suite in test_suites:
        totals["tests"] += suite.get("tests", 0)
        totals["passed"] += suite.get("passed", 0)
        totals["skipped"] += suite.get("skipped", 0)
        totals["failed"] += suite.get("failed", 0)
        totals["errored"] += suite.get("errored", 0)

    return json.dumps(
        {
            "job": build.get("job_name", ""),
            "result": build.get("result", ""),
            "test_suites": test_suites,
            "suite_count": len(test_suites),
            "totals": totals,
        }
    )

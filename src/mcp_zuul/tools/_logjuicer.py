"""LogJuicer integration (optional, requires LOGJUICER_URL)."""

import json
from urllib.parse import quote

from mcp.server.fastmcp import Context

from ..errors import handle_errors
from ..helpers import api, app, clean, error, safepath
from ..server import mcp
from ._common import _READ_ONLY, _no_log_url_error, _resolve


@mcp.tool(title="Log Anomaly Detection", annotations=_READ_ONLY)
@handle_errors
async def get_build_anomalies(
    ctx: Context,
    uuid: str = "",
    tenant: str = "",
    url: str = "",
) -> str:
    """Detect anomalous log lines using LogJuicer ML-based analysis.

    Compares failed build logs against successful baselines to find
    lines that are unusual. Requires LOGJUICER_URL to be configured.
    Accepts a build UUID or Zuul build URL.

    Args:
        uuid: Build UUID
        tenant: Tenant name (uses default if empty)
        url: Zuul build URL (alternative to uuid + tenant)
    """
    a = app(ctx)
    if not a.config.logjuicer_url:
        return error("LogJuicer not configured (set LOGJUICER_URL)")

    uuid, t = _resolve(ctx, uuid, tenant, url, "build")
    build = await api(ctx, f"/tenant/{safepath(t)}/build/{safepath(uuid)}")
    log_url = build.get("log_url")
    if not log_url:
        return _no_log_url_error(build, uuid)

    # Build the Zuul build URL for LogJuicer
    build_url = f"{a.config.base_url}/t/{quote(t, safe='/')}/build/{quote(uuid)}"

    # Request a LogJuicer report — use log_client (no auth headers)
    # to avoid leaking Zuul tokens to the LogJuicer host
    report_url = f"{a.config.logjuicer_url}/api/report/new"
    resp = await a.log_client.put(
        report_url,
        params={"target": build_url, "errors": "true"},
        follow_redirects=True,
    )
    if resp.status_code != 200:
        return error(f"LogJuicer report creation failed: {resp.status_code}")

    report_data = resp.json()
    report_id = str(report_data.get("id") or report_data.get("report_id") or "")
    if not report_id:
        return error("LogJuicer returned no report ID")
    # Sanitize report_id to prevent path traversal in the URL
    if "/" in report_id or ".." in report_id:
        return error("LogJuicer returned invalid report ID")

    # Fetch the report JSON
    report_resp = await a.log_client.get(
        f"{a.config.logjuicer_url}/api/report/{report_id}/json",
        follow_redirects=True,
    )
    if report_resp.status_code != 200:
        return error(f"LogJuicer report fetch failed: {report_resp.status_code}")

    report = report_resp.json()
    anomalies = []
    for source in report if isinstance(report, list) else [report]:
        for anomaly in source.get("anomalies", []):
            anomalies.append(
                clean(
                    {
                        "line": anomaly.get("line"),
                        "pos": anomaly.get("pos"),
                        "before": anomaly.get("before"),
                        "after": anomaly.get("after"),
                    }
                )
            )

    return json.dumps(
        {
            "job": build.get("job_name", ""),
            "result": build.get("result", ""),
            "report_id": report_id,
            "anomaly_count": len(anomalies),
            "anomalies": anomalies[:50],
        }
    )

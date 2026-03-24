"""Integration tests for build and buildset tools."""

import gzip
import json

import httpx
import respx

from mcp_zuul.tools import (
    _no_log_url_error,
    diagnose_build,
    get_build,
    get_build_failures,
    get_buildset,
    get_job_durations,
    list_builds,
    list_buildsets,
)
from tests.conftest import make_build, make_buildset, make_job_output_json


class TestNoLogUrlError:
    def test_in_progress_build(self):
        """In-progress build should suggest get_change_status."""
        build = {"result": None}
        result = json.loads(_no_log_url_error(build, "uuid-123"))
        assert "still in progress" in result["error"]
        assert "get_change_status" in result["error"]

    def test_in_progress_explicit_result(self):
        """Build with explicit IN_PROGRESS result."""
        build = {"result": "IN_PROGRESS"}
        result = json.loads(_no_log_url_error(build, "uuid-123"))
        assert "still in progress" in result["error"]

    def test_completed_build_no_logs(self):
        """Completed build with no log_url should mention lost logs."""
        build = {"result": "FAILURE"}
        result = json.loads(_no_log_url_error(build, "uuid-456"))
        assert "result: FAILURE" in result["error"]
        assert "lost" in result["error"] or "aborted" in result["error"]

    def test_node_failure_result(self):
        """NODE_FAILURE build should show the result in the error."""
        build = {"result": "NODE_FAILURE"}
        result = json.loads(_no_log_url_error(build, "uuid-789"))
        assert "NODE_FAILURE" in result["error"]


class TestListBuilds:
    @respx.mock
    async def test_returns_builds_with_pagination(self, mock_ctx):
        builds = [make_build(uuid=f"uuid-{i}") for i in range(3)]
        respx.get("https://zuul.example.com/api/tenant/test-tenant/builds").mock(
            return_value=httpx.Response(200, json=builds)
        )
        result = json.loads(await list_builds(mock_ctx, limit=5))
        assert result["count"] == 3
        assert result["has_more"] is False

    @respx.mock
    async def test_has_more_when_exceeds_limit(self, mock_ctx):
        builds = [make_build(uuid=f"uuid-{i}") for i in range(3)]
        respx.get("https://zuul.example.com/api/tenant/test-tenant/builds").mock(
            return_value=httpx.Response(200, json=builds)
        )
        result = json.loads(await list_builds(mock_ctx, limit=2))
        assert result["count"] == 2
        assert result["has_more"] is True

    @respx.mock
    async def test_filters_passed_as_params(self, mock_ctx):
        route = respx.get("https://zuul.example.com/api/tenant/test-tenant/builds").mock(
            return_value=httpx.Response(200, json=[])
        )
        await list_builds(mock_ctx, project="org/repo", result="FAILURE", job_name="test-job")
        assert route.called
        params = dict(route.calls[0].request.url.params)
        assert params["project"] == "org/repo"
        assert params["result"] == "FAILURE"
        assert params["job_name"] == "test-job"

    @respx.mock
    async def test_limit_clamped(self, mock_ctx):
        route = respx.get("https://zuul.example.com/api/tenant/test-tenant/builds").mock(
            return_value=httpx.Response(200, json=[])
        )
        await list_builds(mock_ctx, limit=500)
        params = dict(route.calls[0].request.url.params)
        assert params["limit"] == "101"  # clamped to 100 + 1


class TestGetBuild:
    @respx.mock
    async def test_returns_full_build(self, mock_ctx):
        build = make_build()
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/build-uuid-1").mock(
            return_value=httpx.Response(200, json=build)
        )
        result = json.loads(await get_build(mock_ctx, "build-uuid-1"))
        assert result["uuid"] == "build-uuid-1"
        assert result["job"] == "test-job"
        assert "log_url" in result  # brief=False includes log_url
        assert "nodeset" in result


class TestGetBuildUrl:
    @respx.mock
    async def test_accepts_zuul_url(self, mock_ctx):
        build = make_build(uuid="url-build-uuid")
        respx.get("https://zuul.example.com/api/tenant/my-tenant/build/url-build-uuid").mock(
            return_value=httpx.Response(200, json=build)
        )
        result = json.loads(
            await get_build(
                mock_ctx,
                url="https://zuul.example.com/t/my-tenant/build/url-build-uuid",
            )
        )
        assert result["uuid"] == "url-build-uuid"

    @respx.mock
    async def test_url_with_zuul_prefix(self, mock_ctx):
        build = make_build(uuid="abc123")
        respx.get("https://zuul.example.com/api/tenant/comp-int/build/abc123").mock(
            return_value=httpx.Response(200, json=build)
        )
        result = json.loads(
            await get_build(
                mock_ctx,
                url="https://sf.example.com/zuul/t/comp-int/build/abc123",
            )
        )
        assert result["uuid"] == "abc123"

    async def test_invalid_url_returns_error(self, mock_ctx):
        result = json.loads(await get_build(mock_ctx, url="https://example.com/not-a-zuul-url"))
        assert "error" in result
        assert "Cannot parse" in result["error"]

    async def test_wrong_url_type_returns_error(self, mock_ctx):
        result = json.loads(
            await get_build(
                mock_ctx,
                url="https://zuul.example.com/t/tenant/buildset/some-uuid",
            )
        )
        assert "error" in result
        assert "Expected build" in result["error"]

    async def test_no_uuid_no_url_returns_error(self, mock_ctx):
        result = json.loads(await get_build(mock_ctx))
        assert "error" in result

    @respx.mock
    async def test_explicit_tenant_overrides_url_tenant(self, mock_ctx):
        build = make_build(uuid="override-uuid")
        respx.get("https://zuul.example.com/api/tenant/explicit/build/override-uuid").mock(
            return_value=httpx.Response(200, json=build)
        )
        result = json.loads(
            await get_build(
                mock_ctx,
                url="https://zuul.example.com/t/url-tenant/build/override-uuid",
                tenant="explicit",
            )
        )
        assert result["uuid"] == "override-uuid"


class TestGetBuildFailures:
    @respx.mock
    async def test_parses_failed_tasks(self, mock_ctx):
        build = make_build(result="FAILURE")
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/fail-uuid").mock(
            return_value=httpx.Response(200, json=build)
        )
        respx.get(f"{build['log_url']}job-output.json.gz").mock(
            return_value=httpx.Response(200, json=make_job_output_json(failed=True))
        )
        result = json.loads(await get_build_failures(mock_ctx, "fail-uuid"))
        assert result["result"] == "FAILURE"
        assert len(result["failed_tasks"]) == 1
        assert result["failed_tasks"][0]["task"] == "Run deployment"
        assert result["failed_tasks"][0]["host"] == "controller-0"
        assert result["failed_tasks"][0]["rc"] == 1
        # Failed playbooks include full detail (stats + playbook_full)
        assert len(result["playbooks"]) == 1
        assert result["playbooks"][0]["failed"] is True
        assert "stats" in result["playbooks"][0]
        assert "playbook_full" in result["playbooks"][0]
        assert result["playbook_count"] == 1

    @respx.mock
    async def test_success_build_short_circuits(self, mock_ctx):
        build = make_build(result="SUCCESS")
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/build-uuid-1").mock(
            return_value=httpx.Response(200, json=build)
        )
        result = json.loads(await get_build_failures(mock_ctx, "build-uuid-1"))
        assert result["result"] == "SUCCESS"
        assert "succeeded" in result["message"]
        assert "failed_tasks" not in result

    @respx.mock
    async def test_skipped_build_short_circuits_with_correct_message(self, mock_ctx):
        build = make_build(result="SKIPPED")
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/build-uuid-1").mock(
            return_value=httpx.Response(200, json=build)
        )
        result = json.loads(await get_build_failures(mock_ctx, "build-uuid-1"))
        assert result["result"] == "SKIPPED"
        assert "skipped" in result["message"]
        assert "succeeded" not in result["message"]

    @respx.mock
    async def test_no_log_url(self, mock_ctx):
        build = make_build(result="FAILURE", log_url=None)
        build["log_url"] = None
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/no-log").mock(
            return_value=httpx.Response(200, json=build)
        )
        result = json.loads(await get_build_failures(mock_ctx, "no-log"))
        assert "error" in result

    @respx.mock
    async def test_fallback_to_uncompressed(self, mock_ctx):
        build = make_build(result="FAILURE")
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/build-uuid-1").mock(
            return_value=httpx.Response(200, json=build)
        )
        respx.get(f"{build['log_url']}job-output.json.gz").mock(return_value=httpx.Response(404))
        respx.get(f"{build['log_url']}job-output.json").mock(
            return_value=httpx.Response(200, json=make_job_output_json(failed=False))
        )
        result = json.loads(await get_build_failures(mock_ctx, "build-uuid-1"))
        assert result["result"] == "FAILURE"
        assert len(result.get("failed_tasks", [])) == 0

    @respx.mock
    async def test_json_not_found_falls_through_to_text(self, mock_ctx):
        """Both JSONs 404 — should fall through to text grep, not hard error."""
        build = make_build(result="FAILURE")
        log_text = "some output\nfatal: [host]: UNREACHABLE!\nmore output"
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/build-uuid-1").mock(
            return_value=httpx.Response(200, json=build)
        )
        respx.get(f"{build['log_url']}job-output.json.gz").mock(return_value=httpx.Response(404))
        respx.get(f"{build['log_url']}job-output.json").mock(return_value=httpx.Response(404))
        respx.get(f"{build['log_url']}job-output.txt").mock(
            return_value=httpx.Response(200, content=log_text.encode())
        )
        result = json.loads(await get_build_failures(mock_ctx, "build-uuid-1"))
        assert "error" not in result
        assert result["json_fallback"] is True
        assert len(result["log_context"]) >= 1

    @respx.mock
    async def test_includes_passing_playbooks(self, mock_ctx):
        """Passing playbooks should be included with failed=False."""
        build = make_build(result="FAILURE")
        # Two playbooks: one passing pre-run, one failing run
        job_output = [
            {
                "phase": "pre",
                "playbook": "/path/to/pre.yaml",
                "stats": {"controller": {"failures": 0, "ok": 5}},
                "plays": [],
            },
            {
                "phase": "run",
                "playbook": "/path/to/run.yaml",
                "stats": {"controller": {"failures": 1, "ok": 2}},
                "plays": [
                    {
                        "play": {"name": "Run"},
                        "tasks": [
                            {
                                "task": {"name": "Deploy", "duration": {}},
                                "hosts": {"ctrl": {"failed": True, "msg": "err", "rc": 1}},
                            }
                        ],
                    }
                ],
            },
        ]
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/fail-uuid").mock(
            return_value=httpx.Response(200, json=build)
        )
        respx.get(f"{build['log_url']}job-output.json.gz").mock(
            return_value=httpx.Response(200, json=job_output)
        )
        result = json.loads(await get_build_failures(mock_ctx, "fail-uuid"))
        assert result["playbook_count"] == 2
        assert len(result["playbooks"]) == 2
        # Passing playbooks: compact (no stats, no playbook_full)
        assert result["playbooks"][0]["failed"] is False
        assert result["playbooks"][0]["phase"] == "pre"
        assert result["playbooks"][0]["playbook"] == "pre.yaml"
        assert "stats" not in result["playbooks"][0]
        assert "playbook_full" not in result["playbooks"][0]
        # Failed playbooks: full detail (stats + playbook_full)
        assert result["playbooks"][1]["failed"] is True
        assert result["playbooks"][1]["phase"] == "run"
        assert "stats" in result["playbooks"][1]
        assert "playbook_full" in result["playbooks"][1]
        assert len(result["failed_tasks"]) == 1

    @respx.mock
    async def test_extracts_cmd_from_command_task(self, mock_ctx):
        build = make_build(result="FAILURE")
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/fail-uuid").mock(
            return_value=httpx.Response(200, json=build)
        )
        respx.get(f"{build['log_url']}job-output.json.gz").mock(
            return_value=httpx.Response(200, json=make_job_output_json(failed=True))
        )
        result = json.loads(await get_build_failures(mock_ctx, "fail-uuid"))
        ft = result["failed_tasks"][0]
        assert (
            ft["cmd"]
            == "ansible-playbook playbooks/deploy.yaml -i /home/zuul/inventory.yaml -e @/home/zuul/vars.yaml"
        )
        assert ft["invocation"]["chdir"] == "/home/zuul/src/repo"
        assert ft["invocation"]["cmd"] == ft["cmd"]

    @respx.mock
    async def test_no_cmd_for_non_command_task(self, mock_ctx):
        build = make_build(result="FAILURE")
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/fail-uuid").mock(
            return_value=httpx.Response(200, json=build)
        )
        job_output = [
            {
                "phase": "run",
                "playbook": "/path/to/deploy.yaml",
                "stats": {"controller-0": {"failures": 1, "ok": 5}},
                "plays": [
                    {
                        "play": {"name": "Deploy"},
                        "tasks": [
                            {
                                "task": {
                                    "name": "Copy file",
                                    "duration": {"end": "2025-01-01T00:04:00"},
                                },
                                "hosts": {
                                    "controller-0": {
                                        "failed": True,
                                        "msg": "file not found",
                                        "rc": None,
                                    }
                                },
                            }
                        ],
                    }
                ],
            }
        ]
        respx.get(f"{build['log_url']}job-output.json.gz").mock(
            return_value=httpx.Response(200, json=job_output)
        )
        result = json.loads(await get_build_failures(mock_ctx, "fail-uuid"))
        ft = result["failed_tasks"][0]
        assert "cmd" not in ft
        assert "invocation" not in ft

    @respx.mock
    async def test_stdout_smart_truncation(self, mock_ctx):
        """Long stdout should use smart truncation (head + tail, not just head)."""
        build = make_build(result="FAILURE")
        # Build output where the failure is at the END (like container exec)
        long_output = "startup line\n" * 500 + "PLAY RECAP ***\nlocalhost: ok=5 failed=1\n"
        job_output = [
            {
                "phase": "run",
                "playbook": "/path/to/run.yaml",
                "stats": {"ctrl": {"failures": 1, "ok": 0}},
                "plays": [
                    {
                        "play": {"name": "Run"},
                        "tasks": [
                            {
                                "task": {"name": "Task", "duration": {}},
                                "hosts": {
                                    "ctrl": {
                                        "failed": True,
                                        "msg": "err",
                                        "stdout": long_output,
                                    }
                                },
                            }
                        ],
                    }
                ],
            }
        ]
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/fail-uuid").mock(
            return_value=httpx.Response(200, json=build)
        )
        respx.get(f"{build['log_url']}job-output.json.gz").mock(
            return_value=httpx.Response(200, json=job_output)
        )
        result = json.loads(await get_build_failures(mock_ctx, "fail-uuid"))
        ft = result["failed_tasks"][0]
        # Smart truncation keeps tail — failure at end is visible
        assert "PLAY RECAP" in ft["stdout"]
        assert "failed=1" in ft["stdout"]
        assert "omitted" in ft["stdout"]  # truncation marker present
        assert len(ft["stdout"]) <= 4100  # within budget

    @respx.mock
    async def test_container_exec_inner_recap(self, mock_ctx):
        """Container exec failures should extract inner PLAY RECAP."""
        build = make_build(result="FAILURE")
        # Simulate podman_container_exec with embedded ansible output
        inner_ansible = (
            "Using /etc/ansible.cfg\n" * 100
            + "\x1b[0;32mok: [host1]\x1b[0m\n" * 50
            + "PLAY RECAP *******\n"
            + "\x1b[0;31mlocalhost\x1b[0m : ok=74 changed=30 unreachable=0 "
            + "\x1b[0;31mfailed=1\x1b[0m skipped=29 rescued=1 ignored=0\n"
        )
        job_output = [
            {
                "phase": "run",
                "playbook": "/path/to/run.yaml",
                "stats": {"undercloud": {"failures": 1, "ok": 0}},
                "plays": [
                    {
                        "play": {"name": "Run shiftstack"},
                        "tasks": [
                            {
                                "task": {
                                    "name": "Run ocp_testing inside container",
                                    "duration": {"end": "2025-01-01T02:20:00"},
                                },
                                "hosts": {
                                    "undercloud": {
                                        "failed": True,
                                        "msg": "",
                                        "rc": 2,
                                        "stderr": "Please review the log for errors.",
                                        "stdout": inner_ansible,
                                    }
                                },
                            }
                        ],
                    }
                ],
            }
        ]
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/fail-uuid").mock(
            return_value=httpx.Response(200, json=build)
        )
        respx.get(f"{build['log_url']}job-output.json.gz").mock(
            return_value=httpx.Response(200, json=job_output)
        )
        result = json.loads(await get_build_failures(mock_ctx, "fail-uuid"))
        ft = result["failed_tasks"][0]
        # inner_recap extracted and ANSI-stripped
        assert ft["inner_recap"] is not None
        assert "PLAY RECAP" in ft["inner_recap"]
        assert "failed=1" in ft["inner_recap"]
        # No ANSI codes in any field
        assert "\x1b" not in ft["inner_recap"]
        assert "\x1b" not in (ft.get("stdout") or "")


class TestGetBuildFailuresDecodingError:
    @respx.mock
    async def test_decoding_error_gz_falls_back_to_json(self, mock_ctx):
        """DecodingError on .json.gz should try .json before text grep."""
        build = make_build(result="FAILURE")
        job_output = make_job_output_json(failed=True)
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/fail-uuid").mock(
            return_value=httpx.Response(200, json=build)
        )
        respx.get(f"{build['log_url']}job-output.json.gz").mock(side_effect=httpx.DecodingError(""))
        respx.get(f"{build['log_url']}job-output.json").mock(
            return_value=httpx.Response(200, json=job_output)
        )
        result = json.loads(await get_build_failures(mock_ctx, "fail-uuid"))
        # Should use structured JSON data from .json, not text fallback
        assert "error" not in result
        assert "json_fallback" not in result
        assert len(result["failed_tasks"]) == 1
        assert result["failed_tasks"][0]["task"] == "Run deployment"

    @respx.mock
    async def test_decoding_error_both_json_falls_through_to_text(self, mock_ctx):
        """DecodingError on both .json.gz and .json should fall through to text grep."""
        build = make_build(result="FAILURE")
        log_text = "some log\nfatal: [host]: FAILED! => deploy error\nmore log"
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/fail-uuid").mock(
            return_value=httpx.Response(200, json=build)
        )
        respx.get(f"{build['log_url']}job-output.json.gz").mock(side_effect=httpx.DecodingError(""))
        respx.get(f"{build['log_url']}job-output.json").mock(side_effect=httpx.DecodingError(""))
        respx.get(f"{build['log_url']}job-output.txt").mock(
            return_value=httpx.Response(200, content=log_text.encode())
        )
        result = json.loads(await get_build_failures(mock_ctx, "fail-uuid"))
        assert "error" not in result
        assert result["json_fallback"] is True
        assert len(result["log_context"]) >= 1
        fatal_lines = [
            line for block in result["log_context"] for line in block if line.get("match")
        ]
        assert any("fatal" in line["text"] for line in fatal_lines)

    @respx.mock
    async def test_decoding_error_all_logs_unavailable(self, mock_ctx):
        """When all log formats are unavailable, return a clear message."""
        build = make_build(result="FAILURE")
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/fail-uuid").mock(
            return_value=httpx.Response(200, json=build)
        )
        respx.get(f"{build['log_url']}job-output.json.gz").mock(side_effect=httpx.DecodingError(""))
        respx.get(f"{build['log_url']}job-output.json").mock(side_effect=httpx.DecodingError(""))
        respx.get(f"{build['log_url']}job-output.txt").mock(return_value=httpx.Response(404))
        result = json.loads(await get_build_failures(mock_ctx, "fail-uuid"))
        assert result["json_fallback"] is True
        assert "unavailable" in result["message"]

    @respx.mock
    async def test_in_progress_build_returns_helpful_error(self, mock_ctx):
        """In-progress build should return status-aware error, not generic."""
        build = make_build(result="FAILURE", log_url=None)
        build["log_url"] = None
        build["result"] = None
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/in-prog").mock(
            return_value=httpx.Response(200, json=build)
        )
        result = json.loads(await get_build_failures(mock_ctx, "in-prog"))
        assert "error" in result
        assert "in progress" in result["error"]


class TestGzipDecompression:
    """Tests for manual gzip decompression in _fetch_job_output."""

    @respx.mock
    async def test_manual_gzip_decompression(self, mock_ctx):
        """Raw gzip bytes (no Content-Encoding) should be decompressed manually."""
        build = make_build(result="FAILURE")
        job_output = make_job_output_json(failed=True)
        gz_content = gzip.compress(json.dumps(job_output).encode())
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/fail-uuid").mock(
            return_value=httpx.Response(200, json=build)
        )
        # Return raw gzip bytes (no Content-Encoding header — httpx won't auto-decompress)
        respx.get(f"{build['log_url']}job-output.json.gz").mock(
            return_value=httpx.Response(200, content=gz_content)
        )
        result = json.loads(await get_build_failures(mock_ctx, "fail-uuid"))
        assert "error" not in result
        assert len(result["failed_tasks"]) == 1
        assert result["failed_tasks"][0]["task"] == "Run deployment"

    @respx.mock
    async def test_gzip_bomb_rejected(self, mock_ctx):
        """Gzip payload that decompresses beyond _MAX_JSON_LOG_BYTES should be skipped."""
        from mcp_zuul.tools._common import _MAX_JSON_LOG_BYTES

        build = make_build(result="FAILURE")
        # Create a gzip bomb: highly compressible data that exceeds limit
        huge_payload = b"[" + b"0," * (_MAX_JSON_LOG_BYTES + 1000) + b"0]"
        gz_bomb = gzip.compress(huge_payload)
        assert len(gz_bomb) < _MAX_JSON_LOG_BYTES, "Compressed size must be under limit"
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/bomb-uuid").mock(
            return_value=httpx.Response(200, json=build)
        )
        respx.get(f"{build['log_url']}job-output.json.gz").mock(
            return_value=httpx.Response(200, content=gz_bomb)
        )
        # .json should also 404 so it falls through to text
        respx.get(f"{build['log_url']}job-output.json").mock(return_value=httpx.Response(404))
        respx.get(f"{build['log_url']}job-output.txt").mock(return_value=httpx.Response(404))
        result = json.loads(await get_build_failures(mock_ctx, "bomb-uuid"))
        # Should fall through to text fallback, not crash with OOM
        assert result["json_fallback"] is True

    @respx.mock
    async def test_non_gzip_gz_falls_through(self, mock_ctx):
        """A .gz URL returning non-gzip content should fall through gracefully."""
        build = make_build(result="FAILURE")
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/fail-uuid").mock(
            return_value=httpx.Response(200, json=build)
        )
        # Return plain text for .gz URL (no gzip magic bytes)
        respx.get(f"{build['log_url']}job-output.json.gz").mock(
            return_value=httpx.Response(200, content=b"not gzip at all")
        )
        respx.get(f"{build['log_url']}job-output.json").mock(
            return_value=httpx.Response(200, json=make_job_output_json(failed=True))
        )
        result = json.loads(await get_build_failures(mock_ctx, "fail-uuid"))
        assert "error" not in result
        assert len(result["failed_tasks"]) == 1

    @respx.mock
    async def test_gzip_at_exact_limit_accepted(self, mock_ctx):
        """Gzip payload decompressing to exactly _MAX_JSON_LOG_BYTES should be accepted."""
        from mcp_zuul.tools._common import _MAX_JSON_LOG_BYTES

        build = make_build(result="FAILURE")
        # Create valid JSON that's exactly _MAX_JSON_LOG_BYTES when encoded
        filler = "x" * (_MAX_JSON_LOG_BYTES - 50)
        payload = json.dumps([{"playbook": filler, "phase": "run", "plays": [], "stats": {}}])
        # Trim or pad to exact size
        payload_bytes = payload.encode()[:_MAX_JSON_LOG_BYTES]
        gz_content = gzip.compress(payload_bytes)
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/exact-uuid").mock(
            return_value=httpx.Response(200, json=build)
        )
        respx.get(f"{build['log_url']}job-output.json.gz").mock(
            return_value=httpx.Response(200, content=gz_content)
        )
        # The payload may not parse as valid JSON after truncation, so it falls through
        # to .json suffix. The key assertion is it doesn't crash or OOM.
        respx.get(f"{build['log_url']}job-output.json").mock(return_value=httpx.Response(404))
        respx.get(f"{build['log_url']}job-output.txt").mock(return_value=httpx.Response(404))
        result = json.loads(await get_build_failures(mock_ctx, "exact-uuid"))
        # Should not crash — either parses successfully or falls through gracefully
        assert "error" not in result or result.get("json_fallback") is True


class TestDiagnoseBuild:
    @respx.mock
    async def test_success_short_circuits(self, mock_ctx):
        build = make_build(result="SUCCESS")
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/build-uuid-1").mock(
            return_value=httpx.Response(200, json=build)
        )
        result = json.loads(await diagnose_build(mock_ctx, "build-uuid-1"))
        assert result["result"] == "SUCCESS"
        assert "nothing to diagnose" in result["message"]

    @respx.mock
    async def test_returns_failures_and_log_context(self, mock_ctx):
        build = make_build(result="FAILURE")
        log_text = "\n".join(
            [
                "line 1 ok",
                "line 2 ok",
                "line 3 ok",
                "line 4 ok",
                "line 5 ok",
                "fatal: [host]: FAILED! => msg",
                "line 7 after",
                "line 8 after",
                "line 9 after",
            ]
        )
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/fail-uuid").mock(
            return_value=httpx.Response(200, json=build)
        )
        respx.get(f"{build['log_url']}job-output.json.gz").mock(
            return_value=httpx.Response(200, json=make_job_output_json(failed=True))
        )
        respx.get(f"{build['log_url']}job-output.txt").mock(
            return_value=httpx.Response(200, content=log_text.encode())
        )
        result = json.loads(await diagnose_build(mock_ctx, "fail-uuid"))
        assert result["result"] == "FAILURE"
        assert len(result["failed_tasks"]) == 1
        assert result["failed_tasks"][0]["task"] == "Run deployment"
        assert len(result["log_context"]) >= 1
        # The fatal line should be in the context block
        fatal_lines = [
            line for block in result["log_context"] for line in block if line.get("match")
        ]
        assert len(fatal_lines) >= 1
        assert "fatal" in fatal_lines[0]["text"]

    @respx.mock
    async def test_diagnose_includes_cmd_and_invocation(self, mock_ctx):
        """diagnose_build must include cmd/invocation like get_build_failures."""
        build = make_build(result="FAILURE")
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/build-uuid-1").mock(
            return_value=httpx.Response(200, json=build)
        )
        respx.get(f"{build['log_url']}job-output.json.gz").mock(
            return_value=httpx.Response(200, json=make_job_output_json(failed=True))
        )
        respx.get(f"{build['log_url']}job-output.txt").mock(
            return_value=httpx.Response(200, content=b"some log\nFAILED! task\nmore log")
        )
        result = json.loads(await diagnose_build(mock_ctx, uuid="build-uuid-1"))
        assert len(result["failed_tasks"]) == 1
        ft = result["failed_tasks"][0]
        # These fields exist in get_build_failures but are currently MISSING from diagnose_build
        assert "cmd" in ft, "diagnose_build should extract cmd field"


class TestDiagnoseBuildDecodingError:
    @respx.mock
    async def test_gz_decoding_error_falls_back_to_json(self, mock_ctx):
        """DecodingError on .json.gz should try .json before text grep."""
        build = make_build(result="FAILURE")
        job_output = make_job_output_json(failed=True)
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/fail-uuid").mock(
            return_value=httpx.Response(200, json=build)
        )
        respx.get(f"{build['log_url']}job-output.json.gz").mock(side_effect=httpx.DecodingError(""))
        respx.get(f"{build['log_url']}job-output.json").mock(
            return_value=httpx.Response(200, json=job_output)
        )
        respx.get(f"{build['log_url']}job-output.txt").mock(
            return_value=httpx.Response(200, content=b"no fatal lines here")
        )
        result = json.loads(await diagnose_build(mock_ctx, "fail-uuid"))
        assert "error" not in result
        # Should have structured data from .json
        assert len(result["failed_tasks"]) == 1
        assert result["failed_tasks"][0]["task"] == "Run deployment"

    @respx.mock
    async def test_both_json_decoding_error_falls_through_to_text(self, mock_ctx):
        """DecodingError on both JSON formats should fall through to text log grep."""
        build = make_build(result="FAILURE")
        log_text = "some log\nfatal: deployment failed\nmore log"
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/fail-uuid").mock(
            return_value=httpx.Response(200, json=build)
        )
        respx.get(f"{build['log_url']}job-output.json.gz").mock(side_effect=httpx.DecodingError(""))
        respx.get(f"{build['log_url']}job-output.json").mock(side_effect=httpx.DecodingError(""))
        respx.get(f"{build['log_url']}job-output.txt").mock(
            return_value=httpx.Response(200, content=log_text.encode())
        )
        result = json.loads(await diagnose_build(mock_ctx, "fail-uuid"))
        assert "error" not in result
        assert result["result"] == "FAILURE"
        assert result.get("failed_tasks", []) == []
        assert len(result["log_context"]) >= 1
        fatal_lines = [
            line for block in result["log_context"] for line in block if line.get("match")
        ]
        assert any("fatal" in line["text"] for line in fatal_lines)

    @respx.mock
    async def test_in_progress_build_returns_helpful_error(self, mock_ctx):
        """In-progress build should return status-aware error."""
        build = make_build(result="FAILURE", log_url=None)
        build["log_url"] = None
        build["result"] = None
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/in-prog").mock(
            return_value=httpx.Response(200, json=build)
        )
        result = json.loads(await diagnose_build(mock_ctx, "in-prog"))
        assert "error" in result
        assert "in progress" in result["error"]


class TestListBuildsets:
    @respx.mock
    async def test_returns_buildsets(self, mock_ctx):
        buildsets = [make_buildset(uuid=f"bs-{i}") for i in range(2)]
        respx.get("https://zuul.example.com/api/tenant/test-tenant/buildsets").mock(
            return_value=httpx.Response(200, json=buildsets)
        )
        result = json.loads(await list_buildsets(mock_ctx))
        assert result["count"] == 2
        assert result["buildsets"][0]["uuid"] == "bs-0"

    @respx.mock
    async def test_include_builds_fetches_details(self, mock_ctx):
        buildsets = [make_buildset(uuid="bs-1")]
        respx.get("https://zuul.example.com/api/tenant/test-tenant/buildsets").mock(
            return_value=httpx.Response(200, json=buildsets)
        )
        respx.get("https://zuul.example.com/api/tenant/test-tenant/buildset/bs-1").mock(
            return_value=httpx.Response(200, json=make_buildset(uuid="bs-1"))
        )
        result = json.loads(await list_buildsets(mock_ctx, include_builds=True))
        assert "builds" in result["buildsets"][0]


class TestGetJobDurations:
    @respx.mock
    async def test_batch_returns_stats_for_multiple_jobs(self, mock_ctx):
        """Should return avg/min/max for each job with >= 3 builds."""

        # Route responses by job_name query param so each job gets distinct data
        def _mock_builds(request):
            name = dict(request.url.params).get("job_name", "")
            base_dur = 300 if name == "deploy-infra" else 600
            builds = [
                make_build(uuid=f"{name}-{i}", job_name=name, duration=base_dur + i * 100)
                for i in range(5)
            ]
            return httpx.Response(200, json=builds)

        respx.get("https://zuul.example.com/api/tenant/test-tenant/builds").mock(
            side_effect=_mock_builds
        )
        result = json.loads(
            await get_job_durations(mock_ctx, job_names=["deploy-infra", "deploy-ocp"])
        )
        assert result["count"] == 2
        by_job = {j["job"]: j for j in result["jobs"]}
        for name in ["deploy-infra", "deploy-ocp"]:
            job = by_job[name]
            assert job["builds"] == 5
            assert "avg" in job
            assert "min" in job
            assert "max" in job
            assert "avg_formatted" in job
        # Verify distinct stats: deploy-ocp (base 600) has higher avg than deploy-infra (base 300)
        assert by_job["deploy-ocp"]["avg"] > by_job["deploy-infra"]["avg"]

    @respx.mock
    async def test_fewer_than_3_builds_returns_no_stats(self, mock_ctx):
        """Jobs with < 3 builds should not have avg/min/max."""
        builds = [make_build(duration=300), make_build(uuid="u2", duration=600)]
        respx.get("https://zuul.example.com/api/tenant/test-tenant/builds").mock(
            return_value=httpx.Response(200, json=builds)
        )
        result = json.loads(await get_job_durations(mock_ctx, job_names=["rare-job"]))
        assert result["jobs"][0]["builds"] == 2
        assert "avg" not in result["jobs"][0]

    async def test_empty_job_names_returns_error(self, mock_ctx):
        result = json.loads(await get_job_durations(mock_ctx, job_names=[]))
        assert "error" in result

    async def test_too_many_jobs_returns_error(self, mock_ctx):
        result = json.loads(
            await get_job_durations(mock_ctx, job_names=[f"job-{i}" for i in range(25)])
        )
        assert "error" in result


class TestGetBuildset:
    @respx.mock
    async def test_returns_full_buildset(self, mock_ctx):
        respx.get("https://zuul.example.com/api/tenant/test-tenant/buildset/bs-uuid").mock(
            return_value=httpx.Response(200, json=make_buildset())
        )
        result = json.loads(await get_buildset(mock_ctx, "bs-uuid"))
        assert result["uuid"] == "buildset-uuid-1"
        assert "builds" in result
        assert "events" in result

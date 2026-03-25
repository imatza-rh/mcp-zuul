"""Tests for files_in_failure and ref_meta in failure responses.

Validates that get_build_failures and diagnose_build surface:
- ref_url, project, change (from the build's ref object)
- files_in_failure (repo-relative file paths extracted from failure output)

These fields help consumers cross-reference failing files against the
change's file list before concluding if a failure is change-related.
"""

import json

import httpx
import respx

from mcp_zuul.tools import (
    _extract_file_paths,
    _ref_meta,
    diagnose_build,
    get_build_failures,
)
from tests.conftest import make_build, make_job_output_json

# ---------------------------------------------------------------------------
# _ref_meta
# ---------------------------------------------------------------------------


class TestRefMeta:
    def test_extracts_ref_fields(self):
        build = make_build(project="org/repo", change=42)
        assert _ref_meta(build) == {
            "ref_url": "https://review.example.com/42",
            "project": "org/repo",
            "change": 42,
        }

    def test_missing_ref_returns_empty(self):
        assert _ref_meta({"ref": None}) == {}

    def test_non_dict_ref_returns_empty(self):
        assert _ref_meta({"ref": "string"}) == {}

    def test_no_ref_key_returns_empty(self):
        assert _ref_meta({}) == {}


# ---------------------------------------------------------------------------
# _extract_file_paths
# ---------------------------------------------------------------------------


class TestExtractFilePaths:
    def test_extracts_from_stdout(self):
        tasks = [{"stdout": "Fixing roles/deploy_loki/README.md\nDone."}]
        assert _extract_file_paths(tasks) == ["roles/deploy_loki/README.md"]

    def test_extracts_from_msg(self):
        tasks = [{"msg": "Could not find roles/foo/tasks/main.yml"}]
        assert _extract_file_paths(tasks) == ["roles/foo/tasks/main.yml"]

    def test_extracts_from_stderr(self):
        tasks = [{"stderr": "ERROR in ci/playbooks/deploy.yaml line 42"}]
        assert _extract_file_paths(tasks) == ["ci/playbooks/deploy.yaml"]

    def test_multiple_files_sorted_unique(self):
        tasks = [
            {
                "stdout": "Fixing roles/b/README.md\nFixing roles/a/main.yml\nFixing roles/b/README.md"
            }
        ]
        result = _extract_file_paths(tasks)
        assert result == ["roles/a/main.yml", "roles/b/README.md"]

    def test_multiple_tasks(self):
        tasks = [
            {"msg": "Error in src/main.py"},
            {"stderr": "Failed at tests/test_auth.py:42"},
        ]
        result = _extract_file_paths(tasks)
        assert "src/main.py" in result
        assert "tests/test_auth.py" in result

    def test_ignores_absolute_system_paths(self):
        """Paths containing /home/, /tmp/, /etc/ should be filtered."""
        tasks = [{"stdout": "Error at /home/zuul/src/repo/file.py"}]
        result = _extract_file_paths(tasks)
        # The path starts with /home/ which is filtered by noise regex
        assert result is None

    def test_ignores_site_packages(self):
        tasks = [{"stderr": "lib/python3.14/site-packages/ansible/errors.py"}]
        assert _extract_file_paths(tasks) is None

    def test_returns_none_when_no_paths(self):
        tasks = [{"msg": "Connection refused", "rc": 1}]
        assert _extract_file_paths(tasks) is None

    def test_returns_none_for_empty_tasks(self):
        assert _extract_file_paths([]) is None

    def test_handles_none_fields(self):
        tasks = [{"msg": None, "stdout": None, "stderr": None}]
        assert _extract_file_paths(tasks) is None

    def test_handles_non_string_fields(self):
        tasks = [{"msg": 42, "stdout": ["list"], "stderr": {"dict": True}}]
        assert _extract_file_paths(tasks) is None

    def test_real_precommit_output(self):
        """The actual output from the conversation that triggered this feature."""
        tasks = [
            {
                "msg": "gmake: *** [Makefile:83: pre_commit_nodeps] Error 1",
                "stdout": (
                    "pre-commit run --all-files 2>&1 | ansi2txt | tee log\n"
                    "trim trailing whitespace.................................................Failed\n"
                    "- hook id: trailing-whitespace\n"
                    "- exit code: 1\n"
                    "- files were modified by this hook\n\n"
                    "Fixing roles/deploy_loki/README.md\n\n"
                    "shellcheck...............................................................Passed\n"
                    "black....................................................................Passed\n"
                    "Ansible-lint.............................................................Passed\n"
                ),
                "rc": 2,
            }
        ]
        result = _extract_file_paths(tasks)
        assert result == ["roles/deploy_loki/README.md"]

    def test_ansible_file_not_found(self):
        tasks = [{"msg": "Could not find or access 'roles/my_role/tasks/main.yml'"}]
        result = _extract_file_paths(tasks)
        assert result == ["roles/my_role/tasks/main.yml"]

    def test_nested_path_with_underscores_and_dashes(self):
        tasks = [{"stdout": "Error in src/mcp_zuul/tools/_builds.py at line 42"}]
        result = _extract_file_paths(tasks)
        assert result == ["src/mcp_zuul/tools/_builds.py"]

    def test_dotfile_directory(self):
        """Dotfile dirs like .github/ and .zuul.d/ are common in CI repos."""
        tasks = [{"msg": "Error in .github/workflows/ci.yml"}]
        assert _extract_file_paths(tasks) == [".github/workflows/ci.yml"]

    def test_dotfile_zuul_d(self):
        tasks = [{"stderr": "Missing .zuul.d/project.yaml"}]
        assert _extract_file_paths(tasks) == [".zuul.d/project.yaml"]

    def test_nested_dotfile_directory(self):
        tasks = [{"msg": "config/.hidden/secrets.yml not found"}]
        assert _extract_file_paths(tasks) == ["config/.hidden/secrets.yml"]

    def test_path_traversal_rejected(self):
        """../../../etc/passwd must not match."""
        tasks = [{"msg": "reading ../../../etc/passwd.txt"}]
        assert _extract_file_paths(tasks) is None

    def test_yaml_and_j2_extensions(self):
        tasks = [
            {"msg": "parse error in scenarios/shiftstack/04-scenario-vars.yaml"},
            {"stderr": "template error roles/foo/templates/bar.conf.j2"},
        ]
        result = _extract_file_paths(tasks)
        assert "roles/foo/templates/bar.conf.j2" in result
        assert "scenarios/shiftstack/04-scenario-vars.yaml" in result


# ---------------------------------------------------------------------------
# get_build_failures integration
# ---------------------------------------------------------------------------


class TestGetBuildFailuresIntegration:
    @respx.mock
    async def test_includes_ref_meta_and_files_in_failure(self, mock_ctx):
        """The key scenario: failure mentions a file, response includes it + ref_url."""
        build = make_build(result="FAILURE", project="org/ci-framework", change=3793)
        # Custom job output where stdout mentions a file path
        job_output = [
            {
                "phase": "run",
                "playbook": "/path/to/run.yaml",
                "stats": {"container": {"failures": 1, "ok": 0}},
                "plays": [
                    {
                        "play": {"name": "Run"},
                        "tasks": [
                            {
                                "task": {"name": "Run check", "duration": {}},
                                "hosts": {
                                    "container": {
                                        "failed": True,
                                        "msg": "gmake: *** Error 1",
                                        "rc": 2,
                                        "stdout": "Fixing roles/deploy_loki/README.md",
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

        # ref_meta present
        assert result["ref_url"] == "https://review.example.com/3793"
        assert result["project"] == "org/ci-framework"
        assert result["change"] == 3793
        # files_in_failure extracted from stdout
        assert result["files_in_failure"] == ["roles/deploy_loki/README.md"]

    @respx.mock
    async def test_no_files_in_failure_when_none_found(self, mock_ctx):
        """When failure output has no file paths, files_in_failure is absent."""
        build = make_build(result="FAILURE")
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/fail-uuid").mock(
            return_value=httpx.Response(200, json=build)
        )
        respx.get(f"{build['log_url']}job-output.json.gz").mock(
            return_value=httpx.Response(200, json=make_job_output_json(failed=True))
        )
        result = json.loads(await get_build_failures(mock_ctx, "fail-uuid"))
        # Default make_job_output_json has no file paths in output
        assert "files_in_failure" not in result

    @respx.mock
    async def test_success_has_no_ref_meta_or_files(self, mock_ctx):
        build = make_build(result="SUCCESS")
        respx.get("https://zuul.example.com/api/tenant/test-tenant/build/build-uuid-1").mock(
            return_value=httpx.Response(200, json=build)
        )
        result = json.loads(await get_build_failures(mock_ctx, "build-uuid-1"))
        assert "ref_url" not in result
        assert "files_in_failure" not in result


# ---------------------------------------------------------------------------
# diagnose_build integration
# ---------------------------------------------------------------------------


class TestDiagnoseBuildIntegration:
    @respx.mock
    async def test_includes_ref_meta_and_files_in_failure(self, mock_ctx):
        build = make_build(result="FAILURE", project="org/repo", change=5678)
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
                                "task": {"name": "Lint", "duration": {}},
                                "hosts": {
                                    "ctrl": {
                                        "failed": True,
                                        "msg": "lint error",
                                        "stderr": "Error in zuul.d/jobs.yaml:42",
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
        respx.get(f"{build['log_url']}job-output.txt").mock(
            return_value=httpx.Response(200, content=b"fatal: lint error")
        )
        result = json.loads(await diagnose_build(mock_ctx, "fail-uuid"))

        assert result["ref_url"] == "https://review.example.com/5678"
        assert result["project"] == "org/repo"
        assert result["change"] == 5678
        assert result["files_in_failure"] == ["zuul.d/jobs.yaml"]

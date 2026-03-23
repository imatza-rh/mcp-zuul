"""Shared test fixtures for mcp-zuul integration tests."""

from unittest.mock import MagicMock

import httpx
import pytest

from mcp_zuul.config import Config
from mcp_zuul.helpers import AppContext


@pytest.fixture
def config():
    """Default test configuration."""
    return Config(
        base_url="https://zuul.example.com",
        default_tenant="test-tenant",
        auth_token=None,
        timeout=30,
        verify_ssl=True,
        use_kerberos=False,
        transport="stdio",
        enabled_tools=None,
        disabled_tools=None,
        host="127.0.0.1",
        port=8000,
        read_only=False,
        logjuicer_url=None,
    )


@pytest.fixture
async def mock_ctx(config):
    """Create a mock MCP Context with AppContext injected."""
    client = httpx.AsyncClient(
        base_url=config.base_url,
        headers={"Accept": "application/json"},
        timeout=config.timeout,
    )
    log_client = httpx.AsyncClient(timeout=config.timeout)
    app_ctx = AppContext(client=client, log_client=log_client, config=config)

    ctx = MagicMock()
    ctx.request_context.lifespan_context = app_ctx
    yield ctx
    await client.aclose()
    await log_client.aclose()


# -- Sample API response factories --


def make_build(
    uuid: str = "build-uuid-1",
    job_name: str = "test-job",
    result: str = "SUCCESS",
    pipeline: str = "check",
    duration: int = 300,
    log_url: str = "https://logs.example.com/build-uuid-1/",
    project: str = "org/repo",
    change: int = 12345,
) -> dict:
    """Create a sample build API response."""
    return {
        "uuid": uuid,
        "job_name": job_name,
        "result": result,
        "pipeline": pipeline,
        "duration": duration,
        "voting": True,
        "start_time": "2025-01-01T00:00:00",
        "end_time": "2025-01-01T00:05:00",
        "event_timestamp": "2025-01-01T00:00:00",
        "log_url": log_url,
        "nodeset": "centos-9-stream",
        "error_detail": None,
        "artifacts": [{"name": "job-output.json", "url": f"{log_url}job-output.json"}],
        "ref": {
            "project": project,
            "change": change,
            "patchset": "1",
            "branch": "main",
            "ref_url": f"https://review.example.com/{change}",
        },
        "buildset": {"uuid": "buildset-uuid-1"},
    }


def make_buildset(
    uuid: str = "buildset-uuid-1",
    result: str = "SUCCESS",
    pipeline: str = "check",
    builds: list | None = None,
) -> dict:
    """Create a sample buildset API response."""
    return {
        "uuid": uuid,
        "result": result,
        "pipeline": pipeline,
        "event_timestamp": "2025-01-01T00:00:00",
        "message": "Build succeeded",
        "first_build_start_time": "2025-01-01T00:00:00",
        "last_build_end_time": "2025-01-01T00:05:00",
        "refs": [
            {"project": "org/repo", "change": 12345, "ref_url": "https://review.example.com/12345"}
        ],
        "builds": builds or [make_build()],
        "events": [{"type": "comment"}],
    }


def make_status_pipeline(
    name: str = "check",
    items: list | None = None,
) -> dict:
    """Create a sample pipeline status response."""
    if items is None:
        items = [make_status_item()]
    return {
        "name": name,
        "change_queues": [{"heads": [items]}],
    }


def make_status_item(
    change: int = 12345,
    active: bool = True,
    jobs: list | None = None,
) -> dict:
    """Create a sample status item."""
    return {
        "id": f"{change},1",
        "active": active,
        "live": True,
        "refs": [
            {
                "project": "org/repo",
                "change": change,
                "ref": f"refs/changes/{change % 100:02d}/{change}/1",
                "id": f"{change},abc123",
                "url": f"https://review.example.com/{change}",
            }
        ],
        "zuul_ref": "Zbuildset-uuid-live",
        "enqueue_time": 1704067200000,
        "jobs": jobs
        or [
            {
                "name": "test-job",
                "uuid": "job-uuid-1",
                "result": None,
                "voting": True,
                "pre_fail": False,
                "elapsed_time": 60000,
                "remaining_time": 240000,
                "estimated_time": 300,
                "start_time": 1704067200,
                "report_url": None,
                "url": "wss://zuul.example.com/console",
                "dependencies": [],
                "waiting_status": None,
                "queued": False,
                "tries": 1,
            }
        ],
        "failing_reasons": [],
    }


def make_chained_status_item(change: int = 12345) -> dict:
    """Create a status item with a realistic adoption chain.

    Chain topology::

                         +-> deploy-ocp (RUNNING, ~13m elapsed, est 109m)
                         |       +-> install-operators (WAITING, est 54m)
        deploy-infra --> |                                                +--> run-adoption (WAITING, est 179m)
        (SUCCESS, 49m)   |                                                |        +-> run-after (WAITING, est 40m)
                         +-> deploy-osp (RUNNING, ~13m elapsed, est 142m)
                                 +-> install-shiftstack (WAITING, est 94m)
    """
    import time as _t

    now = _t.time()
    return {
        "id": f"{change},1",
        "active": True,
        "live": True,
        "refs": [
            {
                "project": "ci-framework/ci-framework-testproject",
                "change": change,
                "ref": f"refs/merge-requests/{change}/head",
                "id": f"{change},abc123",
                "url": f"https://gitlab.example.com/merge_requests/{change}",
            }
        ],
        "zuul_ref": "Zbuildset-chain-uuid",
        "enqueue_time": int((now - 3600) * 1000),
        "jobs": [
            {
                "name": "deploy-infra",
                "uuid": "uuid-infra",
                "result": "SUCCESS",
                "voting": True,
                "elapsed_time": 2940000,  # 49m in ms
                "start_time": now - 3600,
                "dependencies": [],
            },
            {
                "name": "deploy-ocp",
                "uuid": "uuid-ocp",
                "result": None,
                "voting": True,
                "elapsed_time": 780000,  # stale
                "remaining_time": 5760000,  # 96m in ms
                "estimated_time": 6540,  # 109m in seconds
                "start_time": now - 780,
                "dependencies": ["deploy-infra"],
            },
            {
                "name": "deploy-osp",
                "uuid": "uuid-osp",
                "result": None,
                "voting": True,
                "elapsed_time": 780000,
                "remaining_time": 7740000,  # 129m in ms
                "estimated_time": 8520,  # 142m in seconds
                "start_time": now - 780,
                "dependencies": ["deploy-infra"],
            },
            {
                "name": "install-operators",
                "result": None,
                "voting": True,
                "estimated_time": 3240,  # 54m
                "dependencies": ["deploy-ocp"],
                "waiting_status": "dependencies: deploy-ocp",
                "queued": False,
                "tries": 0,
            },
            {
                "name": "install-shiftstack",
                "result": None,
                "voting": True,
                "estimated_time": 5640,  # 94m
                "dependencies": ["deploy-osp"],
                "waiting_status": "dependencies: deploy-osp",
                "queued": False,
                "tries": 0,
            },
            {
                "name": "run-adoption",
                "result": None,
                "voting": True,
                "estimated_time": 10740,  # 179m
                "dependencies": ["install-operators", "install-shiftstack"],
                "waiting_status": "dependencies: install-operators, install-shiftstack",
                "queued": False,
                "tries": 0,
            },
            {
                "name": "run-after",
                "result": None,
                "voting": True,
                "estimated_time": 2400,  # 40m
                "dependencies": ["run-adoption"],
                "waiting_status": "dependencies: run-adoption",
                "queued": False,
                "tries": 0,
            },
        ],
        "failing_reasons": [],
    }


def make_job_output_json(failed: bool = False) -> list:
    """Create a sample job-output.json structure."""
    tasks = []
    if failed:
        tasks = [
            {
                "task": {"name": "Run deployment", "duration": {"end": "2025-01-01T00:04:00"}},
                "hosts": {
                    "controller-0": {
                        "failed": True,
                        "msg": "non-zero return code",
                        "rc": 1,
                        "stderr": "Error: connection refused",
                        "stdout": "",
                        "cmd": "ansible-playbook playbooks/deploy.yaml -i /home/zuul/inventory.yaml -e @/home/zuul/vars.yaml",
                        "invocation": {
                            "module_args": {
                                "cmd": "ansible-playbook playbooks/deploy.yaml -i /home/zuul/inventory.yaml -e @/home/zuul/vars.yaml",
                                "chdir": "/home/zuul/src/repo",
                                "creates": None,
                                "removes": None,
                            }
                        },
                    }
                },
            }
        ]
    return [
        {
            "phase": "run",
            "playbook": "/path/to/deploy.yaml",
            "stats": {"controller-0": {"failures": 1 if failed else 0, "ok": 5}},
            "plays": [
                {
                    "play": {"name": "Deploy services"},
                    "tasks": tasks,
                }
            ],
        }
    ]

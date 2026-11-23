"""Runs against the stack started by `make up`."""

import json
import os
import subprocess

import httpx
import pytest
from temporalio.api.enums.v1 import TaskQueueType
from temporalio.api.taskqueue.v1 import TaskQueue
from temporalio.api.workflowservice.v1 import DescribeNamespaceRequest, DescribeTaskQueueRequest
from temporalio.client import Client

from core.domain.space import Space
from tests.unit.test_compose_ports import ROOT, port_violations

pytestmark = pytest.mark.integration

GATEWAY = os.environ.get("AF_GATEWAY_URL", "http://127.0.0.1:8000")
TEMPORAL = os.environ.get("AF_TEMPORAL_ADDRESS", "127.0.0.1:7233")


async def test_gateway_ready() -> None:
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{GATEWAY}/readyz", timeout=10)
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "checks": {"database": "ok", "temporal": "ok"}}


@pytest.mark.parametrize("space", list(Space))
async def test_space_namespace_exists_and_worker_polls(space: Space) -> None:
    client = await Client.connect(TEMPORAL, namespace=space.temporal_namespace)
    await client.workflow_service.describe_namespace(
        DescribeNamespaceRequest(namespace=space.temporal_namespace)
    )
    response = await client.workflow_service.describe_task_queue(
        DescribeTaskQueueRequest(
            namespace=space.temporal_namespace,
            task_queue=TaskQueue(name=space.task_queue),
            task_queue_type=TaskQueueType.TASK_QUEUE_TYPE_ACTIVITY,
        )
    )
    assert response.pollers, f"no worker polling {space.task_queue}"


def test_rendered_compose_config_binds_loopback() -> None:
    """ADR 0007: checks the config Compose actually uses, including any override files."""
    rendered = subprocess.run(
        ["docker", "compose", "config", "--format", "json"],  # noqa: S607
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    compose = json.loads(rendered)
    assert any(s.get("ports") for s in compose["services"].values())
    assert port_violations(compose) == []

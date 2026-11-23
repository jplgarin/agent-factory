import asyncio
import signal

import structlog
from temporalio.client import Client
from temporalio.worker import Worker

from apps.shared.checks import database_check, temporal_check
from apps.shared.logging import configure_logging
from apps.shared.settings import WorkerSettings
from apps.worker.activities import ping
from apps.worker.health_server import HealthServer

log = structlog.get_logger(__name__)


async def run(settings: WorkerSettings) -> None:
    space = settings.space
    client = await Client.connect(settings.temporal_address, namespace=space.temporal_namespace)
    worker = Worker(client, task_queue=space.task_queue, activities=[ping])

    async def worker_running() -> None:
        if not worker.is_running:
            raise RuntimeError("worker not running")

    health = HealthServer(
        {
            "database": database_check(settings.database_url.get_secret_value()),
            "temporal": temporal_check(client),
            "worker": worker_running,
        },
        settings.check_timeout_s,
    )
    port = await health.start(settings.health_host, settings.health_port)
    log.info("worker_starting", task_queue=space.task_queue, health_port=port)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    worker_task = asyncio.create_task(worker.run())
    stop_task = asyncio.create_task(stop.wait())
    try:
        await asyncio.wait({worker_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        log.info("worker_stopping")
        stop_task.cancel()
        if not worker_task.done():
            await worker.shutdown()
        await health.stop()
    await worker_task


def main() -> None:
    settings = WorkerSettings()  # pyright: ignore[reportCallIssue]
    configure_logging(settings.log_level, service="worker", space=settings.space.value)
    asyncio.run(run(settings))


if __name__ == "__main__":
    main()

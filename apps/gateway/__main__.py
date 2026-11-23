import asyncio

import uvicorn
from temporalio.client import Client

from apps.gateway.app import create_app
from apps.shared.checks import database_check, temporal_check
from apps.shared.logging import configure_logging
from apps.shared.settings import GatewaySettings


async def serve(settings: GatewaySettings) -> None:
    client = await Client.connect(settings.temporal_address, lazy=True)
    checks = {
        "database": database_check(settings.database_url.get_secret_value()),
        "temporal": temporal_check(client),
    }
    app = create_app(checks, settings.check_timeout_s)
    config = uvicorn.Config(app, host=settings.host, port=settings.port, log_config=None)
    await uvicorn.Server(config).serve()


def main() -> None:
    settings = GatewaySettings()  # pyright: ignore[reportCallIssue]
    configure_logging(settings.log_level, service="gateway")
    asyncio.run(serve(settings))


if __name__ == "__main__":
    main()

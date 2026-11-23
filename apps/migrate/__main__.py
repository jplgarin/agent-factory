import structlog

from apps.shared.logging import configure_logging
from db.migrate import upgrade

log = structlog.get_logger(__name__)


def main() -> None:
    configure_logging("INFO", service="migrate")
    upgrade()
    log.info("migrations_applied")


if __name__ == "__main__":
    main()

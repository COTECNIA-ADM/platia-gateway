"""Logs estructurados con structlog: JSON en contenedores, legibles en local."""
import logging

import structlog


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    renderer = structlog.processors.JSONRenderer() if fmt == "json" else structlog.dev.ConsoleRenderer()
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelNamesMapping().get(level.upper(),
                                                                                              logging.INFO)),
        cache_logger_on_first_use=True,
    )

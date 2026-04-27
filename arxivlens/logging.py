"""Structured logging. JSON-formatted in production, pretty in dev."""
import logging
import os
import sys

import structlog


def setup_logging() -> None:
    is_prod = os.environ.get("ENV") == "prod"

    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
    ]

    if is_prod:
        processors = shared_processors + [structlog.processors.JSONRenderer()]
    else:
        processors = shared_processors + [structlog.dev.ConsoleRenderer()]

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
    )


def get_logger(name: str) -> structlog.BoundLogger:
    return structlog.get_logger(name)

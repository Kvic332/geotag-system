"""Structured logging via aws_lambda_powertools (CLAUDE.md coding standards)."""

from __future__ import annotations

from aws_lambda_powertools import Logger

_loggers: dict[str, Logger] = {}


def get_logger(service: str) -> Logger:
    """Return a memoised Powertools logger for `service`."""
    existing = _loggers.get(service)
    if existing is None:
        existing = Logger(service=service)
        _loggers[service] = existing
    return existing

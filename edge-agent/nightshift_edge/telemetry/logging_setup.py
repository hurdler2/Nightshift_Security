"""Structured logging with mandatory credential redaction (spec §33, §45)."""

from __future__ import annotations

import logging

from nightshift_edge.security.redaction import redact_text


class RedactingFilter(logging.Filter):
    """Scrubs `user:pass@` and secret query params from every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_text(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: _scrub(v) for k, v in record.args.items()}
            else:
                record.args = tuple(_scrub(a) for a in record.args)
        return True


def _scrub(value: object) -> object:
    return redact_text(value) if isinstance(value, str) else value


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    handler.addFilter(RedactingFilter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())

"""Structured logging for the core engine.

Log records are emitted as a single line of canonical JSON on the configured
stream so that they can be shipped to any log aggregator without a parser per
event type. Fields are deliberately namespaced (``nga.core.*``) and never
contain variant-level free text produced by a model.

PHI minimization: the logger never records sample identifiers, patient names,
or raw genotype strings. It records *variant identity digests* and evidence
source names. If a deployment needs sample linkage it must add it explicitly
at its own boundary and take responsibility for it.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

LOGGER_NAME = "nga.core"

#: Fields that must never appear in a structured log record, even if a caller
#: passes them. Enforced in :func:`structured_event`.
_BLOCKED_FIELDS = frozenset(
    {
        "sample_id",
        "sample_name",
        "patient_id",
        "patient_name",
        "mrn",
        "accession_number",
        "free_text",
        "llm_output",
    }
)


class StructuredFormatter(logging.Formatter):
    """Render a :class:`logging.LogRecord` as one line of canonical JSON."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": getattr(record, "event", record.getMessage()),
        }
        context = getattr(record, "context", None)
        if isinstance(context, Mapping):
            payload.update(
                {key: value for key, value in context.items() if key not in _BLOCKED_FIELDS})
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, sort_keys=True, default=str)


def configure_logging(stream: Any = None, level: int = logging.INFO) -> logging.Logger:
    """Install the structured formatter on the ``nga.core`` logger.

    Idempotent: repeated calls replace rather than stack handlers, which keeps
    CLI invocations from emitting duplicated lines.
    """
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(StructuredFormatter())
    logger.addHandler(handler)
    return logger


def get_logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)


def structured_event(
    logger: logging.Logger,
    level: int,
    event: str,
    **context: Any,
) -> None:
    """Emit a structured event, dropping any PHI-ish field on the floor.

    Blocked fields are *removed*, not logged-and-redacted, because a redaction
    that depends on every caller remembering the rule is not a control.
    """
    dropped = sorted(key for key in context if key in _BLOCKED_FIELDS)
    safe = {key: value for key, value in context.items() if key not in _BLOCKED_FIELDS}
    if dropped:
        safe["blocked_fields_dropped"] = dropped
    logger.log(level, event, extra={"event": event, "context": safe})

"""Audit trail + structured logging.

Every meaningful step writes one append-only audit row (who, what, when, detail)
and one JSON log line. The audit table is the source of truth for "what did the
automation do and why" — that is what clients and compliance reviews ask for.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

from .db import Database
from .models import utcnow

_LOGGER_NAME = "flow"


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": utcnow(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        extra = getattr(record, "flow", None)
        if extra:
            payload.update(extra)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO", json_logs: bool = True) -> logging.Logger:
    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(level)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(
            JsonFormatter() if json_logs else logging.Formatter("%(levelname)s %(message)s")
        )
        logger.addHandler(handler)
    logger.propagate = False
    return logger


def get_logger() -> logging.Logger:
    return logging.getLogger(_LOGGER_NAME)


# Fields that must never be written to logs or the audit table in clear text.
_REDACT_KEYS = {"api_key", "token", "password", "secret", "authorization"}


def redact(detail: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in detail.items():
        if any(s in k.lower() for s in _REDACT_KEYS):
            out[k] = "***redacted***"
        elif isinstance(v, dict):
            out[k] = redact(v)
        else:
            out[k] = v
    return out


class AuditLog:
    def __init__(self, db: Database, tenant_id: str) -> None:
        self.db = db
        self.tenant_id = tenant_id
        self.log = get_logger()

    def record(
        self,
        event: str,
        *,
        run_id: str | None = None,
        actor: str = "system",
        level: str = "info",
        **detail: Any,
    ) -> None:
        clean = redact(detail)
        self.db.execute(
            "INSERT INTO audit_log (tenant_id, run_id, ts, actor, event, level, detail_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (self.tenant_id, run_id, utcnow(), actor, event, level, json.dumps(clean, default=str)),
        )
        self.log.log(
            logging.ERROR
            if level == "error"
            else logging.WARNING
            if level == "warning"
            else logging.INFO,
            event,
            extra={"flow": {"tenant": self.tenant_id, "run_id": run_id, "actor": actor, **clean}},
        )

    def for_run(self, run_id: str) -> list[dict]:
        rows = self.db.query(
            "SELECT ts, actor, event, level, detail_json FROM audit_log "
            "WHERE tenant_id = ? AND run_id = ? ORDER BY id",
            (self.tenant_id, run_id),
        )
        for r in rows:
            r["detail"] = json.loads(r.pop("detail_json"))
        return rows

    def recent(self, limit: int = 100) -> list[dict]:
        rows = self.db.query(
            "SELECT run_id, ts, actor, event, level, detail_json FROM audit_log "
            "WHERE tenant_id = ? ORDER BY id DESC LIMIT ?",
            (self.tenant_id, limit),
        )
        for r in rows:
            r["detail"] = json.loads(r.pop("detail_json"))
        return rows

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from typing import Any

AUDIT_LOGGER = logging.getLogger("traceforge.audit")


def stable_ref(value: str | None) -> str | None:
    """Return a short, deterministic reference without logging the original identifier."""

    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return f"sha256:{digest[:16]}"


def emit_audit_event(
    event: str,
    outcome: str,
    *,
    component: str,
    actor_ref: str | None = None,
    **fields: Any,
) -> None:
    payload: dict[str, Any] = {
        "schema": "traceforge.audit.v1",
        "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "component": component,
        "event": event,
        "outcome": outcome,
    }
    if actor_ref:
        payload["actor_ref"] = actor_ref
    for key, value in fields.items():
        if value is not None:
            payload[key] = _audit_scalar(value)
    AUDIT_LOGGER.info(
        json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    )


def _audit_scalar(value: Any) -> str | int | float | bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (str, int, float)):
        return value
    raise TypeError(f"audit field values must be scalar, got {type(value).__name__}")

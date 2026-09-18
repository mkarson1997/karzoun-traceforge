from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.common.v1.common_pb2 import KeyValue


@dataclass(frozen=True)
class TenantClaims:
    organization_id: str
    scopes: frozenset[str]
    issued_at: int
    expires_at: int
    token_id: str


class TenantTokenError(ValueError):
    pass


class TenantTokenSigner:
    def __init__(self, signing_key: bytes, *, issuer: str = "traceforge") -> None:
        if len(signing_key) < 32:
            raise ValueError("tenant signing key must be at least 32 bytes")
        self._key = signing_key
        self._issuer = issuer

    def issue(
        self,
        organization_id: str,
        scopes: Iterable[str],
        *,
        ttl_seconds: int = 900,
        now: int | None = None,
    ) -> str:
        if ttl_seconds < 30 or ttl_seconds > 86_400:
            raise ValueError("ttl_seconds must be between 30 and 86400")
        current = int(time.time()) if now is None else int(now)
        normalized_scopes = sorted({scope.strip() for scope in scopes if scope.strip()})
        if not normalized_scopes:
            raise ValueError("at least one scope is required")
        payload = {
            "iss": self._issuer,
            "org": organization_id,
            "scp": normalized_scopes,
            "iat": current,
            "exp": current + ttl_seconds,
            "jti": secrets.token_hex(12),
        }
        encoded = _b64url(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
        signature = _b64url(hmac.new(self._key, encoded.encode(), hashlib.sha256).digest())
        return f"tfv1.{encoded}.{signature}"

    def verify(
        self,
        token: str,
        *,
        required_scope: str | None = None,
        now: int | None = None,
        leeway_seconds: int = 30,
    ) -> TenantClaims:
        parts = token.split(".")
        if len(parts) != 3 or parts[0] != "tfv1":
            raise TenantTokenError("invalid tenant token format")
        encoded, supplied_signature = parts[1], parts[2]
        expected = _b64url(hmac.new(self._key, encoded.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(supplied_signature, expected):
            raise TenantTokenError("invalid tenant token signature")

        try:
            payload = json.loads(_b64url_decode(encoded))
        except (ValueError, json.JSONDecodeError) as exc:
            raise TenantTokenError("invalid tenant token payload") from exc
        if not isinstance(payload, dict):
            raise TenantTokenError("invalid tenant token payload")
        if payload.get("iss") != self._issuer:
            raise TenantTokenError("invalid tenant token issuer")

        organization_id = _required_text(payload, "org")
        token_id = _required_text(payload, "jti")
        issued_at = _required_int(payload, "iat")
        expires_at = _required_int(payload, "exp")
        raw_scopes = payload.get("scp")
        if not isinstance(raw_scopes, list) or not all(isinstance(item, str) for item in raw_scopes):
            raise TenantTokenError("invalid tenant token scopes")
        scopes = frozenset(item.strip() for item in raw_scopes if item.strip())
        current = int(time.time()) if now is None else int(now)
        if issued_at > current + leeway_seconds:
            raise TenantTokenError("tenant token is not valid yet")
        if expires_at <= current - leeway_seconds:
            raise TenantTokenError("tenant token has expired")
        if required_scope and required_scope not in scopes:
            raise TenantTokenError(f"tenant token is missing required scope: {required_scope}")
        return TenantClaims(
            organization_id=organization_id,
            scopes=scopes,
            issued_at=issued_at,
            expires_at=expires_at,
            token_id=token_id,
        )


def bearer_token_from_metadata(metadata: Iterable[Any]) -> str | None:
    for item in metadata:
        key = str(getattr(item, "key", item[0] if isinstance(item, tuple) else "")).lower()
        value = str(getattr(item, "value", item[1] if isinstance(item, tuple) else ""))
        if key == "authorization" and value.lower().startswith("bearer "):
            token = value[7:].strip()
            return token or None
    return None


def enforce_tenant_identity(
    request: ExportTraceServiceRequest,
    organization_id: str,
) -> None:
    for resource_spans in request.resource_spans:
        attributes = resource_spans.resource.attributes
        retained: list[KeyValue] = []
        for item in attributes:
            if item.key in {
                "traceforge.organization.id",
                "traceforge.tenant.id",
                "traceforge.policy.organization_id",
            }:
                continue
            copy = KeyValue()
            copy.CopyFrom(item)
            retained.append(copy)
        del attributes[:]
        attributes.extend(retained)
        candidate = attributes.add()
        candidate.key = "traceforge.organization.id"
        candidate.value.string_value = organization_id


def _required_text(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise TenantTokenError(f"invalid tenant token field: {key}")
    return value.strip()


def _required_int(payload: Mapping[str, Any], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise TenantTokenError(f"invalid tenant token field: {key}")
    return value


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _b64url_decode(value: str) -> str:
    padding = "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode(value + padding).decode()
    except (ValueError, UnicodeDecodeError) as exc:
        raise TenantTokenError("invalid tenant token payload encoding") from exc

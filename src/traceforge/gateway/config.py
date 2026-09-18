from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from traceforge.gateway.client_auth import parse_certificate_hashes
from traceforge.privacy import PrivacyPolicy, RedactionMode


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return default if raw is None else float(raw)


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return default if raw is None else int(raw)


@dataclass(frozen=True)
class GatewayConfig:
    listen_address: str = "0.0.0.0:4317"
    health_address: str = "0.0.0.0:8080"
    upstream_endpoint: str = "127.0.0.1:4319"
    upstream_insecure: bool = True
    upstream_ca_file: Path | None = None
    upstream_client_cert_file: Path | None = None
    upstream_client_key_file: Path | None = None
    max_receive_message_mib: int = 16
    export_timeout_seconds: float = 10.0
    max_inflight_exports: int = 64
    admission_timeout_seconds: float = 0.25
    trusted_client_certificate_hashes: frozenset[str] = frozenset()
    client_rate_limit_per_minute: int = 0
    rate_limit_max_clients: int = 4096
    redaction_mode: RedactionMode = RedactionMode.REDACT
    redact_pii: bool = True
    detect_high_entropy: bool = True
    tokenization_key: bytes | None = None
    tenant_signing_key: bytes | None = None
    tenant_auth_required: bool = False

    def __post_init__(self) -> None:
        if self.max_receive_message_mib < 1:
            raise ValueError("max_receive_message_mib must be at least 1")
        if self.export_timeout_seconds <= 0:
            raise ValueError("export_timeout_seconds must be greater than 0")
        if self.max_inflight_exports < 1:
            raise ValueError("max_inflight_exports must be at least 1")
        if self.admission_timeout_seconds <= 0:
            raise ValueError("admission_timeout_seconds must be greater than 0")
        if self.client_rate_limit_per_minute < 0:
            raise ValueError("client_rate_limit_per_minute must be zero or greater")
        if self.rate_limit_max_clients < 1:
            raise ValueError("rate_limit_max_clients must be at least 1")
        if self.tenant_signing_key is not None and len(self.tenant_signing_key) < 32:
            raise ValueError("tenant_signing_key must be at least 32 bytes")
        if self.tenant_auth_required and self.tenant_signing_key is None:
            raise ValueError("tenant_auth_required needs TRACEFORGE_TENANT_SIGNING_KEY")

    @classmethod
    def from_env(cls) -> GatewayConfig:
        mode = RedactionMode(os.getenv("TRACEFORGE_REDACTION_MODE", "redact"))
        raw_key = os.getenv("TRACEFORGE_TOKENIZATION_KEY")
        raw_tenant_key = os.getenv("TRACEFORGE_TENANT_SIGNING_KEY")
        return cls(
            listen_address=os.getenv("TRACEFORGE_LISTEN_ADDRESS", "0.0.0.0:4317"),
            health_address=os.getenv("TRACEFORGE_HEALTH_ADDRESS", "0.0.0.0:8080"),
            upstream_endpoint=os.getenv(
                "TRACEFORGE_UPSTREAM_OTLP_ENDPOINT", "127.0.0.1:4319"
            ),
            upstream_insecure=_env_bool("TRACEFORGE_UPSTREAM_INSECURE", True),
            upstream_ca_file=_optional_path("TRACEFORGE_UPSTREAM_CA_FILE"),
            upstream_client_cert_file=_optional_path(
                "TRACEFORGE_UPSTREAM_CLIENT_CERT_FILE"
            ),
            upstream_client_key_file=_optional_path(
                "TRACEFORGE_UPSTREAM_CLIENT_KEY_FILE"
            ),
            max_receive_message_mib=_env_int("TRACEFORGE_MAX_RECEIVE_MIB", 16),
            export_timeout_seconds=_env_float("TRACEFORGE_EXPORT_TIMEOUT_SECONDS", 10.0),
            max_inflight_exports=_env_int("TRACEFORGE_MAX_INFLIGHT_EXPORTS", 64),
            admission_timeout_seconds=_env_float(
                "TRACEFORGE_ADMISSION_TIMEOUT_SECONDS", 0.25
            ),
            trusted_client_certificate_hashes=parse_certificate_hashes(
                os.getenv("TRACEFORGE_TRUSTED_CLIENT_CERT_HASHES")
            ),
            client_rate_limit_per_minute=_env_int(
                "TRACEFORGE_CLIENT_RATE_LIMIT_PER_MINUTE", 0
            ),
            rate_limit_max_clients=_env_int("TRACEFORGE_RATE_LIMIT_MAX_CLIENTS", 4096),
            redaction_mode=mode,
            redact_pii=_env_bool("TRACEFORGE_REDACT_PII", True),
            detect_high_entropy=_env_bool("TRACEFORGE_DETECT_HIGH_ENTROPY", True),
            tokenization_key=raw_key.encode("utf-8") if raw_key else None,
            tenant_signing_key=raw_tenant_key.encode("utf-8") if raw_tenant_key else None,
            tenant_auth_required=_env_bool("TRACEFORGE_TENANT_AUTH_REQUIRED", False),
        )

    def privacy_policy(self) -> PrivacyPolicy:
        return PrivacyPolicy(
            mode=self.redaction_mode,
            redact_pii=self.redact_pii,
            detect_high_entropy=self.detect_high_entropy,
        )


def _optional_path(name: str) -> Path | None:
    value = os.getenv(name)
    return Path(value) if value else None

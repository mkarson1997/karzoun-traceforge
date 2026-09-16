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
    redaction_mode: RedactionMode = RedactionMode.REDACT
    redact_pii: bool = True
    detect_high_entropy: bool = True
    tokenization_key: bytes | None = None

    def __post_init__(self) -> None:
        if self.max_receive_message_mib < 1:
            raise ValueError("max_receive_message_mib must be at least 1")
        if self.export_timeout_seconds <= 0:
            raise ValueError("export_timeout_seconds must be greater than 0")
        if self.max_inflight_exports < 1:
            raise ValueError("max_inflight_exports must be at least 1")
        if self.admission_timeout_seconds <= 0:
            raise ValueError("admission_timeout_seconds must be greater than 0")

    @classmethod
    def from_env(cls) -> GatewayConfig:
        mode = RedactionMode(os.getenv("TRACEFORGE_REDACTION_MODE", "redact"))
        raw_key = os.getenv("TRACEFORGE_TOKENIZATION_KEY")
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
            redaction_mode=mode,
            redact_pii=_env_bool("TRACEFORGE_REDACT_PII", True),
            detect_high_entropy=_env_bool("TRACEFORGE_DETECT_HIGH_ENTROPY", True),
            tokenization_key=raw_key.encode("utf-8") if raw_key else None,
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

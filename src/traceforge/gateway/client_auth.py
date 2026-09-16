from __future__ import annotations

import hmac
from collections.abc import Iterable
from typing import Any

_HEX_DIGITS = frozenset("0123456789ABCDEF")


def normalize_certificate_hash(value: str) -> str:
    """Normalize a SHA-256 certificate thumbprint to 64 uppercase hex characters."""

    normalized = "".join(character for character in value.upper() if character not in ":- ")
    if len(normalized) != 64 or any(character not in _HEX_DIGITS for character in normalized):
        raise ValueError("client certificate hash must be a SHA-256 thumbprint")
    return normalized


def parse_certificate_hashes(raw: str | None) -> frozenset[str]:
    if not raw:
        return frozenset()
    return frozenset(
        normalize_certificate_hash(item)
        for item in raw.split(",")
        if item.strip()
    )


def forwarded_client_certificate_hash(metadata: Iterable[Any]) -> str | None:
    """Extract the SHA-256 Hash field from Azure Container Apps XFCC metadata."""

    for item in metadata:
        key, value = _metadata_pair(item)
        if key.lower() != "x-forwarded-client-cert":
            continue
        for field in value.split(";"):
            name, separator, field_value = field.partition("=")
            if separator and name.strip().lower() == "hash":
                candidate = field_value.strip().strip('"')
                try:
                    return normalize_certificate_hash(candidate)
                except ValueError:
                    return None
    return None


def client_certificate_authorized(
    metadata: Iterable[Any],
    trusted_hashes: frozenset[str],
) -> bool:
    if not trusted_hashes:
        return True
    presented = forwarded_client_certificate_hash(metadata)
    if presented is None:
        return False
    return any(hmac.compare_digest(presented, expected) for expected in trusted_hashes)


def _metadata_pair(item: Any) -> tuple[str, str]:
    key = getattr(item, "key", None)
    value = getattr(item, "value", None)
    if key is not None and value is not None:
        return str(key), _metadata_value(value)
    try:
        tuple_key, tuple_value = item
    except (TypeError, ValueError):
        return "", ""
    return str(tuple_key), _metadata_value(tuple_value)


def _metadata_value(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="ignore")
    return str(value)

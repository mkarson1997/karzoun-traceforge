import pytest

from traceforge.gateway.client_auth import (
    client_certificate_authorized,
    forwarded_client_certificate_hash,
    normalize_certificate_hash,
    parse_certificate_hashes,
)


def test_certificate_hash_normalization_accepts_common_thumbprint_format() -> None:
    raw = ":".join(["ab"] * 32)
    assert normalize_certificate_hash(raw) == "AB" * 32


def test_certificate_hash_parser_rejects_non_sha256_values() -> None:
    with pytest.raises(ValueError, match="SHA-256"):
        normalize_certificate_hash("abcd")


def test_forwarded_client_certificate_hash_reads_aca_xfcc_header() -> None:
    expected = "CD" * 32
    metadata = [
        (
            "x-forwarded-client-cert",
            f'Hash={expected};Cert="-----BEGIN CERTIFICATE-----...";Chain="...";',
        )
    ]
    assert forwarded_client_certificate_hash(metadata) == expected


def test_client_certificate_authorization_supports_rotation_allowlist() -> None:
    old_hash = "11" * 32
    new_hash = "22" * 32
    trusted = parse_certificate_hashes(f"{old_hash},{new_hash}")

    assert client_certificate_authorized(
        [("x-forwarded-client-cert", f"Hash={old_hash}")], trusted
    )
    assert client_certificate_authorized(
        [("x-forwarded-client-cert", f"Hash={new_hash}")], trusted
    )
    assert not client_certificate_authorized(
        [("x-forwarded-client-cert", f"Hash={'33' * 32}")], trusted
    )
    assert not client_certificate_authorized([], trusted)

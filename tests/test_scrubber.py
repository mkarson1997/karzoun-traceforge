from traceforge.privacy import PrivacyPolicy, RedactionMode, Scrubber


def test_redacts_common_secrets_and_pii() -> None:
    payload = {
        "message": (
            "contact dev@example.com using Bearer abcdefghijklmnopqrstuvwxyz123456 "
            "or github_pat_11AA22BB33CC44DD55EE66FF77GG88HH"
        ),
        "client_ip": "10.20.30.40",
    }
    result = Scrubber().scrub(payload)

    text = str(result.value)
    assert "dev@example.com" not in text
    assert "abcdefghijklmnopqrstuvwxyz123456" not in text
    assert "github_pat_" not in text
    assert "10.20.30.40" not in text
    assert {finding.kind for finding in result.findings} >= {
        "email",
        "bearer_token",
        "github_token",
        "ipv4",
    }


def test_drops_prompt_and_source_content_keys() -> None:
    payload = {
        "service.name": "demo",
        "gen_ai.prompt": "please paste secret data",
        "source.content": "print('sensitive')",
        "safe.attribute": "kept",
    }
    result = Scrubber().scrub(payload)

    assert result.value == {"service.name": "demo", "safe.attribute": "kept"}
    assert len(result.findings) == 2
    assert all(finding.kind == "sensitive_key" for finding in result.findings)


def test_tokenization_is_stable_and_does_not_leak_plaintext() -> None:
    policy = PrivacyPolicy(mode=RedactionMode.TOKENIZE)
    scrubber = Scrubber(policy, tokenization_key=b"unit-test-key")

    first = scrubber.scrub("owner=dev@example.com").value
    second = scrubber.scrub("owner=dev@example.com").value

    assert first == second
    assert "dev@example.com" not in first
    assert "[TOKEN:email:" in first


def test_detects_high_entropy_tokens() -> None:
    token = "aZ9Qv7Lm2Pk8Rt4Xn6Yw3Bc5Df1Gh0Jk"
    result = Scrubber().scrub({"opaque": token})

    assert token not in result.value["opaque"]
    assert any(finding.kind == "high_entropy_token" for finding in result.findings)


def test_preserves_benign_telemetry() -> None:
    payload = {
        "service.name": "traceforge-demo",
        "http.method": "GET",
        "http.status_code": 200,
        "tags": ["demo", "otel"],
    }
    result = Scrubber().scrub(payload)

    assert result.value == payload
    assert result.findings == ()


def test_nested_payload_is_scrubbed_recursively() -> None:
    payload = {
        "event": {
            "attributes": {
                "user.email": "person@example.com",
                "message": "email person@example.com",
            }
        }
    }
    result = Scrubber().scrub(payload)

    attrs = result.value["event"]["attributes"]
    assert attrs["user.email"].startswith("[REDACTED:")
    assert "person@example.com" not in attrs["message"]

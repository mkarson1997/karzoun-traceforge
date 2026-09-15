from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import hashlib
import hmac
import math
import re
from typing import Any, Mapping


class RedactionMode(StrEnum):
    REDACT = "redact"
    DROP = "drop"
    TOKENIZE = "tokenize"


@dataclass(frozen=True)
class Finding:
    kind: str
    path: str
    detail: str


@dataclass(frozen=True)
class ScrubResult:
    value: Any
    findings: tuple[Finding, ...] = ()


@dataclass(frozen=True)
class PrivacyPolicy:
    mode: RedactionMode = RedactionMode.REDACT
    redact_pii: bool = True
    detect_high_entropy: bool = True
    entropy_threshold: float = 4.2
    entropy_min_length: int = 24
    sensitive_key_fragments: tuple[str, ...] = (
        "authorization",
        "cookie",
        "set-cookie",
        "password",
        "passwd",
        "secret",
        "api_key",
        "apikey",
        "access_token",
        "refresh_token",
        "client_secret",
        "prompt",
        "completion",
        "response_body",
        "request_body",
        "source.content",
        "source.code",
        "code.content",
        "db.statement",
        "enduser.id",
        "user.email",
    )
    drop_key_fragments: tuple[str, ...] = (
        "source.content",
        "source.code",
        "code.content",
        "db.statement",
        "prompt",
        "completion",
        "request_body",
        "response_body",
    )


@dataclass(frozen=True)
class _PatternRule:
    kind: str
    pattern: re.Pattern[str]


_SECRET_RULES: tuple[_PatternRule, ...] = (
    _PatternRule(
        "github_token",
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,255}|github_pat_[A-Za-z0-9_]{20,255})\b"),
    ),
    _PatternRule("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    _PatternRule(
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    ),
    _PatternRule(
        "bearer_token",
        re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{16,}"),
    ),
    _PatternRule(
        "generic_secret_assignment",
        re.compile(
            r"(?i)\b(?:api[_-]?key|token|secret|password|passwd|client[_-]?secret)\b"
            r"\s*[:=]\s*[\"']?[^\s,;\"']{8,}[\"']?"
        ),
    ),
)

_PII_RULES: tuple[_PatternRule, ...] = (
    _PatternRule(
        "email",
        re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE),
    ),
    _PatternRule(
        "ipv4",
        re.compile(
            r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}"
            r"(?:25[0-5]|2[0-4]\d|1?\d?\d)\b"
        ),
    ),
    _PatternRule(
        "phone",
        re.compile(r"(?<!\w)(?:\+?\d[\d .()\-]{7,}\d)(?!\w)"),
    ),
)

_ENTROPY_CANDIDATE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9_+\-/=]{24,}(?![A-Za-z0-9])")


class Scrubber:
    """Scrub secrets and PII from arbitrary telemetry-like data structures.

    The scrubber is deliberately independent of OpenTelemetry protobufs. Adapters can
    convert OTel attributes, events, resource attributes, or JSON payloads to native
    Python values and apply the same policy consistently.
    """

    def __init__(
        self,
        policy: PrivacyPolicy | None = None,
        *,
        tokenization_key: bytes | None = None,
    ) -> None:
        self.policy = policy or PrivacyPolicy()
        self._tokenization_key = tokenization_key
        if self.policy.mode is RedactionMode.TOKENIZE and not tokenization_key:
            raise ValueError("tokenization_key is required when mode=tokenize")

    def scrub(self, value: Any, *, path: str = "$") -> ScrubResult:
        findings: list[Finding] = []
        scrubbed = self._scrub_value(value, path=path, findings=findings)
        return ScrubResult(value=scrubbed, findings=tuple(findings))

    def _scrub_value(self, value: Any, *, path: str, findings: list[Finding]) -> Any:
        if isinstance(value, str):
            return self._scrub_text(value, path=path, findings=findings)
        if isinstance(value, Mapping):
            return self._scrub_mapping(value, path=path, findings=findings)
        if isinstance(value, tuple):
            return tuple(
                self._scrub_value(item, path=f"{path}[{index}]", findings=findings)
                for index, item in enumerate(value)
            )
        if isinstance(value, list):
            return [
                self._scrub_value(item, path=f"{path}[{index}]", findings=findings)
                for index, item in enumerate(value)
            ]
        return value

    def _scrub_mapping(
        self,
        value: Mapping[Any, Any],
        *,
        path: str,
        findings: list[Finding],
    ) -> dict[Any, Any]:
        output: dict[Any, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            child_path = f"{path}.{key_text}"
            normalized = key_text.lower().replace("-", "_")

            if self._contains_fragment(normalized, self.policy.drop_key_fragments):
                findings.append(Finding("sensitive_key", child_path, "dropped"))
                continue

            if self._contains_fragment(normalized, self.policy.sensitive_key_fragments):
                findings.append(Finding("sensitive_key", child_path, self.policy.mode.value))
                if self.policy.mode is RedactionMode.DROP:
                    continue
                output[key] = self._replacement(str(item), "sensitive_key")
                continue

            output[key] = self._scrub_value(item, path=child_path, findings=findings)
        return output

    def _scrub_text(self, text: str, *, path: str, findings: list[Finding]) -> str:
        result = text
        for rule in _SECRET_RULES:
            result = self._replace_rule(result, rule, path=path, findings=findings)

        if self.policy.redact_pii:
            for rule in _PII_RULES:
                result = self._replace_rule(result, rule, path=path, findings=findings)

        if self.policy.detect_high_entropy:
            result = self._replace_entropy(result, path=path, findings=findings)

        return result

    def _replace_rule(
        self,
        text: str,
        rule: _PatternRule,
        *,
        path: str,
        findings: list[Finding],
    ) -> str:
        def replacer(match: re.Match[str]) -> str:
            findings.append(Finding(rule.kind, path, "pattern_match"))
            return self._replacement(match.group(0), rule.kind)

        return rule.pattern.sub(replacer, text)

    def _replace_entropy(self, text: str, *, path: str, findings: list[Finding]) -> str:
        def replacer(match: re.Match[str]) -> str:
            candidate = match.group(0)
            if len(candidate) < self.policy.entropy_min_length:
                return candidate
            entropy = _shannon_entropy(candidate)
            if entropy < self.policy.entropy_threshold:
                return candidate
            findings.append(Finding("high_entropy_token", path, f"entropy={entropy:.2f}"))
            return self._replacement(candidate, "high_entropy_token")

        return _ENTROPY_CANDIDATE.sub(replacer, text)

    def _replacement(self, value: str, kind: str) -> str:
        if self.policy.mode is RedactionMode.TOKENIZE:
            assert self._tokenization_key is not None
            digest = hmac.new(
                self._tokenization_key,
                value.encode("utf-8", errors="replace"),
                hashlib.sha256,
            ).hexdigest()[:20]
            return f"[TOKEN:{kind}:{digest}]"
        return f"[REDACTED:{kind}]"

    @staticmethod
    def _contains_fragment(value: str, fragments: tuple[str, ...]) -> bool:
        return any(fragment in value for fragment in fragments)


def _shannon_entropy(value: str) -> float:
    if not value:
        return 0.0
    counts: dict[str, int] = {}
    for char in value:
        counts[char] = counts.get(char, 0) + 1
    length = len(value)
    return -sum((count / length) * math.log2(count / length) for count in counts.values())

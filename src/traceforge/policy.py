from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

_SOURCE_ALIASES = {
    "codex": "codex",
    "claude": "claude-code",
    "claude-code": "claude-code",
    "copilot": "github-copilot",
    "github-copilot": "github-copilot",
    "generic": "generic",
}
_ALL_SOURCES = frozenset(_SOURCE_ALIASES.values())
_ORG_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


@dataclass(frozen=True)
class OrganizationPolicy:
    organization_id: str
    profile: str
    allowed_adapters: frozenset[str]
    capture_model_names: bool
    capture_tool_names: bool
    capture_usage_metrics: bool
    max_batch_size: int

    def allows(self, adapter: str) -> bool:
        return normalize_adapter_name(adapter) in self.allowed_adapters

    def require_allowed(self, adapter: str) -> None:
        normalized = normalize_adapter_name(adapter)
        if normalized not in self.allowed_adapters:
            raise ValueError(
                f"adapter {adapter!r} is not allowed by policy {self.profile!r}"
            )

    def filter_attributes(
        self,
        attributes: dict[str, str | int | bool | float],
    ) -> dict[str, str | int | bool | float]:
        result = dict(attributes)
        if not self.capture_model_names:
            result.pop("gen_ai.request.model", None)
            result.pop("gen_ai.response.model", None)
        if not self.capture_tool_names:
            result.pop("tool.name", None)
            result.pop("tool.names", None)
        if not self.capture_usage_metrics:
            for key in tuple(result):
                if key.startswith("gen_ai.usage."):
                    result.pop(key, None)
        return result

    def metadata(self) -> dict[str, str | int | bool]:
        return {
            "traceforge.organization.id": self.organization_id,
            "traceforge.policy.profile": self.profile,
            "traceforge.policy.max_batch_size": self.max_batch_size,
        }


_PROFILE_DEFAULTS: dict[str, OrganizationPolicy] = {
    "minimal": OrganizationPolicy(
        organization_id="org-default",
        profile="minimal",
        allowed_adapters=_ALL_SOURCES,
        capture_model_names=False,
        capture_tool_names=False,
        capture_usage_metrics=False,
        max_batch_size=25,
    ),
    "strict": OrganizationPolicy(
        organization_id="org-default",
        profile="strict",
        allowed_adapters=_ALL_SOURCES,
        capture_model_names=False,
        capture_tool_names=False,
        capture_usage_metrics=True,
        max_batch_size=50,
    ),
    "standard": OrganizationPolicy(
        organization_id="org-default",
        profile="standard",
        allowed_adapters=_ALL_SOURCES,
        capture_model_names=True,
        capture_tool_names=True,
        capture_usage_metrics=True,
        max_batch_size=100,
    ),
}


def builtin_policy(profile: str, organization_id: str) -> OrganizationPolicy:
    normalized_profile = profile.strip().lower()
    if normalized_profile not in _PROFILE_DEFAULTS:
        choices = ", ".join(sorted(_PROFILE_DEFAULTS))
        raise ValueError(f"unknown policy profile {profile!r}; choose one of: {choices}")
    validate_organization_id(organization_id)
    return replace(
        _PROFILE_DEFAULTS[normalized_profile],
        organization_id=organization_id,
    )


def load_policy(path: str | Path) -> OrganizationPolicy:
    source = Path(path)
    payload = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("policy file must contain a JSON object")

    organization_id = str(payload.get("organization_id") or "").strip()
    profile = str(payload.get("profile") or "strict").strip().lower()
    policy = builtin_policy(profile, organization_id)

    allowed = payload.get("allowed_adapters")
    if allowed is not None:
        if not isinstance(allowed, list) or not allowed:
            raise ValueError("allowed_adapters must be a non-empty JSON array")
        normalized_allowed = frozenset(normalize_adapter_name(str(item)) for item in allowed)
        policy = replace(policy, allowed_adapters=normalized_allowed)

    capture = payload.get("capture")
    if capture is not None:
        if not isinstance(capture, dict):
            raise ValueError("capture must be a JSON object")
        policy = replace(
            policy,
            capture_model_names=_bool_override(
                capture,
                "model_names",
                policy.capture_model_names,
            ),
            capture_tool_names=_bool_override(
                capture,
                "tool_names",
                policy.capture_tool_names,
            ),
            capture_usage_metrics=_bool_override(
                capture,
                "usage_metrics",
                policy.capture_usage_metrics,
            ),
        )

    if "max_batch_size" in payload:
        value = payload["max_batch_size"]
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("max_batch_size must be an integer")
        if not 1 <= value <= 1000:
            raise ValueError("max_batch_size must be between 1 and 1000")
        policy = replace(policy, max_batch_size=value)

    return policy


def normalize_adapter_name(value: str) -> str:
    normalized = value.strip().lower()
    try:
        return _SOURCE_ALIASES[normalized]
    except KeyError as exc:
        choices = ", ".join(sorted(_SOURCE_ALIASES))
        raise ValueError(f"unknown adapter {value!r}; choose one of: {choices}") from exc


def validate_organization_id(value: str) -> None:
    if not value:
        raise ValueError("organization_id is required")
    if not _ORG_ID.fullmatch(value):
        raise ValueError(
            "organization_id must be 1-64 characters using letters, numbers, dot, dash, or underscore"
        )


def render_policy(policy: OrganizationPolicy) -> dict[str, Any]:
    value = asdict(policy)
    value["allowed_adapters"] = sorted(policy.allowed_adapters)
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate and inspect TraceForge organization policy")
    parser.add_argument("policy_file", type=Path)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        policy = load_policy(args.policy_file)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"traceforge-policy: {exc}")
        return 2
    print(json.dumps(render_policy(policy), indent=2, sort_keys=True))
    return 0


def _bool_override(source: dict[str, Any], key: str, default: bool) -> bool:
    if key not in source:
        return default
    value = source[key]
    if not isinstance(value, bool):
        raise ValueError(f"capture.{key} must be true or false")
    return value


if __name__ == "__main__":
    raise SystemExit(main())

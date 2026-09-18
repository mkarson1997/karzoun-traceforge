from __future__ import annotations

import json

import pytest

from traceforge.policy import builtin_policy, load_policy, render_policy


def test_builtin_strict_policy_filters_model_and_tool_names():
    policy = builtin_policy("strict", "org_demo")
    filtered = policy.filter_attributes(
        {
            "gen_ai.request.model": "model-x",
            "tool.name": "shell",
            "gen_ai.usage.input_tokens": 12,
            "safe.count": 4,
        }
    )

    assert "gen_ai.request.model" not in filtered
    assert "tool.name" not in filtered
    assert filtered["gen_ai.usage.input_tokens"] == 12
    assert filtered["safe.count"] == 4


def test_minimal_policy_drops_usage_metadata():
    policy = builtin_policy("minimal", "org_demo")
    filtered = policy.filter_attributes(
        {
            "gen_ai.usage.input_tokens": 12,
            "gen_ai.usage.output_tokens": 4,
            "safe.count": 1,
        }
    )

    assert filtered == {"safe.count": 1}


def test_policy_file_overrides_adapter_and_capture_controls(tmp_path):
    path = tmp_path / "policy.json"
    path.write_text(
        json.dumps(
            {
                "organization_id": "org_acme",
                "profile": "strict",
                "allowed_adapters": ["codex", "claude"],
                "capture": {
                    "model_names": True,
                    "tool_names": False,
                    "usage_metrics": False,
                },
                "max_batch_size": 20,
            }
        ),
        encoding="utf-8",
    )

    policy = load_policy(path)
    assert policy.organization_id == "org_acme"
    assert policy.allows("codex")
    assert policy.allows("claude-code")
    assert not policy.allows("copilot")
    assert policy.capture_model_names is True
    assert policy.capture_tool_names is False
    assert policy.capture_usage_metrics is False
    assert policy.max_batch_size == 20

    rendered = render_policy(policy)
    assert rendered["allowed_adapters"] == ["claude-code", "codex"]


def test_policy_rejects_invalid_organization_id():
    with pytest.raises(ValueError, match="organization_id"):
        builtin_policy("strict", "bad org id")


def test_policy_rejects_disallowed_adapter():
    policy = builtin_policy("standard", "org_demo")
    restricted = policy.__class__(
        organization_id=policy.organization_id,
        profile=policy.profile,
        allowed_adapters=frozenset({"codex"}),
        capture_model_names=policy.capture_model_names,
        capture_tool_names=policy.capture_tool_names,
        capture_usage_metrics=policy.capture_usage_metrics,
        max_batch_size=policy.max_batch_size,
    )

    with pytest.raises(ValueError, match="not allowed"):
        restricted.require_allowed("copilot")

# Organization policy profiles

TraceForge policy profiles provide a small, deterministic organization-control layer for agent
telemetry before OTLP export.

A policy file is JSON. It identifies an organization, selects a built-in profile, optionally
restricts allowed adapters, and can override bounded metadata capture settings.

Example:

    {
      "organization_id": "org_acme",
      "profile": "strict",
      "allowed_adapters": ["codex", "claude"],
      "capture": {
        "model_names": false,
        "tool_names": false,
        "usage_metrics": true
      },
      "max_batch_size": 50
    }

Validate and inspect the effective policy:

    traceforge-policy ./traceforge-policy.json

Use it with an adapter:

    traceforge-adapter codex       --policy-file ./traceforge-policy.json       --endpoint 127.0.0.1:4317       --insecure

The adapter refuses a provider that is not listed in allowed_adapters. It also refuses a batch
size larger than max_batch_size.

## Built-in profiles

minimal:
- model names are not captured
- tool names are not captured
- token/cost usage metadata is not captured
- maximum batch size is 25

strict:
- model names are not captured
- tool names are not captured
- token/cost usage metadata is allowed
- maximum batch size is 50

standard:
- model names are allowed
- tool names are allowed
- token/cost usage metadata is allowed
- maximum batch size is 100

All profiles still pass normalized metadata through the TraceForge privacy scrubber. Prompts,
assistant text, command strings, working-directory paths, tool arguments, and tool results remain
outside the adapter's bounded metadata model.

## Organization identifiers

organization_id is required and must be 1 to 64 characters using letters, numbers, dot, dash, or
underscore. Use an opaque stable ID rather than a human-readable customer name when possible.

The effective organization ID and policy profile are attached as resource-level telemetry metadata:

    traceforge.organization.id
    traceforge.policy.profile
    traceforge.policy.max_batch_size

These fields prepare the telemetry plane for the M7 multi-tenant control plane without changing the
existing storage privacy invariant.

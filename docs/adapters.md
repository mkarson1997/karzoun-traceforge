# Agent adapters

TraceForge adapters normalize coding-agent event streams into a small vendor-neutral event model,
scrub adapter metadata before serialization, and forward sanitized OpenTelemetry spans to the
privacy gateway.

Supported adapters are Codex, Claude Code, GitHub Copilot hooks, and a generic JSON/JSONL format.

The adapters deliberately do not copy prompt text, assistant message text, command strings,
tool arguments, tool results, working-directory paths, or arbitrary unknown provider fields.
They retain correlation IDs, event types, tool names, model identifiers, token counters, timing,
status, and other bounded metadata. The resulting attributes are passed through the TraceForge
privacy scrubber before OTLP serialization, while the gateway remains the durable privacy boundary.

## CLI

Codex JSON Lines:

    codex exec --json "inspect the repository" |
      traceforge-adapter codex --endpoint 127.0.0.1:4317 --insecure

Claude Code stream JSON:

    claude -p "inspect the repository" --output-format stream-json --verbose |
      traceforge-adapter claude --endpoint 127.0.0.1:4317 --insecure

GitHub Copilot hooks can pipe the JSON hook payload on stdin:

    traceforge-adapter copilot --endpoint 127.0.0.1:4317 --insecure

Generic JSONL example:

    {"type":"tool.completed","session_id":"s1","task_id":"t1","status":"completed","attributes":{"tool.name":"shell","safe.count":4}}

Then run:

    traceforge-adapter generic --input ./events.jsonl       --endpoint 127.0.0.1:4317 --insecure

For TLS or mTLS, omit --insecure and optionally provide --ca-file, --client-cert-file,
and --client-key-file.

Use --dry-run to exercise parsing, normalization, and privacy scrubbing without exporting OTLP.

## Generic event contract

Recognized top-level fields include name/event/type, timestamp, session_id/sessionId,
task_id/taskId, status, model, tool_name, service, agent, and attributes.

Unknown top-level fields are ignored. Generic attributes are accepted and then passed through
the existing privacy scrubber before an OTLP request is constructed.

## Correlation

Each adapter span carries traceforge.agent.name plus available session/task IDs. Trace IDs are
deterministic within the adapter, session, and task tuple so events for the same task correlate
naturally in the existing TraceForge viewer without persisting raw prompts or tool payloads.

## Organization policy

Pass --policy-file to enforce an organization policy profile before any OTLP batch is exported.

    traceforge-adapter codex --policy-file ./traceforge-policy.json       --endpoint 127.0.0.1:4317 --insecure

The policy can restrict adapters, cap batch size, and suppress model names, tool names, or usage
metadata. See docs/policies.md for the full contract.

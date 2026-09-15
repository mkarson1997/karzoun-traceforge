# Local trace data model

The local reference store is intentionally small and auditable. It persists only telemetry that has already crossed the TraceForge privacy boundary, then applies the same scrubber a second time at ingestion as defense in depth.

## Correlation model

```text
task
  └─ session
      └─ trace
          ├─ root span
          ├─ planning span
          ├─ tool spans
          └─ completion span
```

TraceForge recognizes these correlation attributes in priority order:

- Session: `traceforge.session.id`, `session.id`, `gen_ai.conversation.id`
- Task: `traceforge.task.id`, `task.id`, `gen_ai.request.id`
- Agent: `traceforge.agent.name`, `agent.name`, `gen_ai.system`

The correlation fields are copied into indexed SQLite columns so the viewer never needs to scan arbitrary attribute JSON to group sessions and tasks.

## SQLite schema

The reference store uses one normalized `spans` table with a composite primary key of `(trace_id, span_id)`. Repeated OTLP delivery is therefore idempotent: a retry updates the same span instead of duplicating it.

Stored fields include:

- trace/span/parent identifiers
- span name, kind, status and duration
- task/session/agent/service correlation fields
- sanitized resource and span attributes
- sanitized events and links
- ingestion timestamp

SQLite runs in WAL mode for practical concurrent read/write behavior during the local demo.

## Privacy invariants

Persistent storage must never be the first privacy boundary. The expected path is:

```text
agent -> edge collector -> privacy gateway -> central collector -> trace store
```

The local store re-runs the privacy scrubber before writing as a second barrier. CI includes a synthetic trace containing a fake email address, GitHub token, bearer token, prompt, completion, request body, and source-code value. The regression test fails if any of those raw values reach SQLite.

The viewer is read-only. It exposes no endpoint for altering or replaying traces, and it sends restrictive browser security headers.

## Production evolution

SQLite is the local reference profile, not the final cloud persistence strategy. The Azure profile will keep the same correlation model while routing sanitized telemetry to Azure Data Explorer/Kusto for queryable traces and Blob/ADLS for policy-controlled archival datasets.

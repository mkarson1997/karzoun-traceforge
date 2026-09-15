# Threat model

## Protected assets

- Source code and code fragments
- Prompts and model responses
- Access tokens, API keys, cookies, connection strings, and credentials
- Developer identity and personally identifiable information
- Repository names and local filesystem paths when policy marks them sensitive
- Trace correlation identifiers and operational metadata

## Primary threats

| Threat | Example | Required control |
|---|---|---|
| Secret exfiltration | PAT copied into a span attribute | Pattern detection, entropy detection, sensitive-key rules, pre-storage scrubbing |
| Prompt/code leakage | Raw prompt or source file emitted as telemetry | Drop by default |
| Bypass through nested payloads | Secret inside event attributes or JSON metadata | Recursive scrubbing |
| Over-privileged viewer | User can alter traces or policy | Read-only query identity and RBAC |
| Collector exposure | Local OTLP port reachable from LAN | Loopback binding by default |
| Interception | OTLP crosses network in plaintext | TLS/mTLS in non-local deployments |
| Correlation abuse | Tokenized values reversible by guessing | HMAC tokenization with protected high-entropy key |
| Policy drift | Collector config changed without review | Versioned policy, CI tests, signed releases |
| Storage contamination | Unscrubbed telemetry reaches durable backend | Privacy gateway is mandatory and central exporters accept only gateway output |

## Non-goals for the first milestone

- Perfect semantic understanding of every possible secret format
- Full data-loss-prevention classification using external ML services
- Multi-tenant billing and commercial control plane

These are later hardening areas, not excuses to weaken the default privacy boundary.

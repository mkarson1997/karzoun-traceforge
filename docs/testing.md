# Reliability and benchmark testing

TraceForge separates correctness tests from performance evidence. CI must remain deterministic, while load and soak runs collect measurements without encoding brittle hardware-specific throughput thresholds.

## Privacy microbenchmark

The privacy benchmark repeatedly clones the synthetic coding-agent OTLP request, runs the real privacy scrubber, and scans the serialized sanitized request for the deliberately injected regression values.

```bash
traceforge-benchmark privacy --iterations 2000 --output privacy-benchmark.json
```

The command exits non-zero if any known synthetic email, credential, request body, source fragment, or completion survives the scrub. The JSON result records throughput, p50/p95/p99 scrub latency, spans processed, privacy findings, and leak count.

## Gateway load test

Against the local Docker reference stack:

```bash
docker compose up --build -d
traceforge-benchmark gateway \
  --endpoint 127.0.0.1:4317 \
  --insecure \
  --requests 10000 \
  --concurrency 32 \
  --output gateway-load.json
```

The result records completed requests, observed gRPC status counts, throughput, and latency percentiles. `RESOURCE_EXHAUSTED` is a meaningful result when admission or per-client limits are deliberately exceeded; the benchmark does not hide it with automatic retries.

## Soak test

Use a duration instead of a fixed request count:

```bash
traceforge-benchmark gateway \
  --endpoint 127.0.0.1:4317 \
  --insecure \
  --duration-seconds 900 \
  --concurrency 16 \
  --output gateway-soak.json
```

For the production Azure gateway, omit `--insecure` and supply the trust/client material when mTLS is enabled:

```bash
traceforge-benchmark gateway \
  --endpoint <gateway-fqdn>:443 \
  --ca-file ./ca.pem \
  --client-cert-file ./client.pem \
  --client-key-file ./client-key.pem \
  --duration-seconds 300 \
  --concurrency 16
```

Never commit client certificates or private keys to the repository.

## Failure injection

`tests/test_failure_injection.py` starts a real in-process gRPC upstream that deliberately returns `UNAVAILABLE`. The privacy gateway must propagate a fail-closed `UNAVAILABLE` response, release its admission slot, and increment its upstream-failure metric rather than pretending the export was stored.

The existing gateway tests additionally inject admission saturation, missing/untrusted mTLS identities, and per-client rate-limit exhaustion.

## Scheduled evidence

`.github/workflows/benchmarks.yml` runs weekly and can also be started manually. It performs:

1. the privacy microbenchmark with leak assertions,
2. deterministic failure injection tests,
3. a complete Docker Compose pipeline startup,
4. a concurrent gateway load test,
5. a timed gateway soak test,
6. final Prometheus-style gateway metric capture,
7. service-log capture and artifact retention for 30 days.

The manual workflow input controls soak duration. Measurements are evidence, not universal performance guarantees, because GitHub-hosted runner capacity varies over time.

## Reading results

Investigate these patterns rather than chasing one vanity throughput number:

- rising p95/p99 latency with stable request volume,
- unexpected `UNAVAILABLE` responses,
- sustained `traceforge_gateway_backpressure_active 1`,
- growth in `traceforge_gateway_rate_limit_rejections_total` not explained by policy,
- non-zero privacy leak count,
- memory or process instability during longer soak runs,
- sanitized store/export content diverging from privacy regression expectations.

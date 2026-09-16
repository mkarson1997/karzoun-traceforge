from __future__ import annotations

import argparse
import asyncio
import json
import time
from collections import Counter
from pathlib import Path
from typing import Any

import grpc
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)
from opentelemetry.proto.collector.trace.v1.trace_service_pb2_grpc import TraceServiceStub

from traceforge.demo import build_demo_request
from traceforge.gateway.otel_scrub import scrub_trace_export_request
from traceforge.privacy import Scrubber

_PRIVACY_MARKERS = (
    b"developer@example.com",
    b"ghp_abcdefghijklmnopqrstuvwxyz123456",
    b"Bearer abcdefghijklmnopqrstuvwxyz0123456789",
    b"customer secret body",
    b"sensitive source code",
    b"private generated completion",
)


def run_privacy_benchmark(iterations: int = 1000) -> dict[str, Any]:
    iterations = max(1, iterations)
    template, _ = build_demo_request(
        trace_id=bytes.fromhex("ab" * 16),
        session_id="benchmark-session",
        task_id="benchmark-task",
        start_ns=1_000_000_000,
    )
    scrubber = Scrubber()
    latencies_ms: list[float] = []
    total_findings = 0
    total_spans = 0
    leaked_markers: set[str] = set()

    started = time.perf_counter()
    for _ in range(iterations):
        request = ExportTraceServiceRequest()
        request.CopyFrom(template)
        request_started = time.perf_counter()
        stats = scrub_trace_export_request(request, scrubber)
        latencies_ms.append((time.perf_counter() - request_started) * 1000)
        total_findings += stats.findings
        total_spans += stats.spans_seen
        serialized = request.SerializeToString()
        for marker in _PRIVACY_MARKERS:
            if marker in serialized:
                leaked_markers.add(marker.decode("utf-8"))

    duration = time.perf_counter() - started
    return {
        "mode": "privacy",
        "iterations": iterations,
        "duration_seconds": round(duration, 6),
        "requests_per_second": round(iterations / duration, 3) if duration else 0.0,
        "latency_ms_p50": round(_percentile(latencies_ms, 0.50), 6),
        "latency_ms_p95": round(_percentile(latencies_ms, 0.95), 6),
        "latency_ms_p99": round(_percentile(latencies_ms, 0.99), 6),
        "spans_scrubbed": total_spans,
        "privacy_findings": total_findings,
        "privacy_leaks": len(leaked_markers),
        "leaked_markers": sorted(leaked_markers),
    }


async def run_gateway_benchmark(
    endpoint: str,
    *,
    requests: int = 1000,
    concurrency: int = 16,
    duration_seconds: float = 0.0,
    timeout_seconds: float = 10.0,
    insecure: bool = False,
    ca_file: Path | None = None,
    client_cert_file: Path | None = None,
    client_key_file: Path | None = None,
) -> dict[str, Any]:
    if concurrency < 1:
        raise ValueError("concurrency must be at least 1")
    if requests < 1 and duration_seconds <= 0:
        raise ValueError("requests must be at least 1 when duration_seconds is not set")
    if bool(client_cert_file) != bool(client_key_file):
        raise ValueError("client certificate and key must be supplied together")

    channel = _gateway_channel(
        endpoint,
        insecure=insecure,
        ca_file=ca_file,
        client_cert_file=client_cert_file,
        client_key_file=client_key_file,
    )
    stub = TraceServiceStub(channel)
    latencies_ms: list[float] = []
    statuses: Counter[str] = Counter()
    completed = 0
    next_request = 0
    lock = asyncio.Lock()
    started = time.perf_counter()
    deadline = started + duration_seconds if duration_seconds > 0 else None

    async def claim_request() -> int | None:
        nonlocal next_request
        async with lock:
            if deadline is not None:
                if time.perf_counter() >= deadline:
                    return None
            elif next_request >= requests:
                return None
            value = next_request
            next_request += 1
            return value

    async def worker() -> None:
        nonlocal completed
        while True:
            index = await claim_request()
            if index is None:
                return
            trace_id = (index + 1).to_bytes(16, "big", signed=False)
            request, _ = build_demo_request(
                trace_id=trace_id,
                session_id=f"benchmark-session-{index % max(1, concurrency)}",
                task_id=f"benchmark-task-{index}",
            )
            request_started = time.perf_counter()
            try:
                await stub.Export(request, timeout=timeout_seconds)
            except grpc.aio.AioRpcError as exc:
                statuses[exc.code().name] += 1
            else:
                statuses["OK"] += 1
            finally:
                latencies_ms.append((time.perf_counter() - request_started) * 1000)
                completed += 1

    try:
        await asyncio.gather(*(worker() for _ in range(concurrency)))
    finally:
        await channel.close()

    elapsed = time.perf_counter() - started
    return {
        "mode": "gateway",
        "endpoint": endpoint,
        "concurrency": concurrency,
        "completed_requests": completed,
        "duration_seconds": round(elapsed, 6),
        "requests_per_second": round(completed / elapsed, 3) if elapsed else 0.0,
        "latency_ms_p50": round(_percentile(latencies_ms, 0.50), 6),
        "latency_ms_p95": round(_percentile(latencies_ms, 0.95), 6),
        "latency_ms_p99": round(_percentile(latencies_ms, 0.99), 6),
        "statuses": dict(sorted(statuses.items())),
    }


def _gateway_channel(
    endpoint: str,
    *,
    insecure: bool,
    ca_file: Path | None,
    client_cert_file: Path | None,
    client_key_file: Path | None,
) -> grpc.aio.Channel:
    if insecure:
        return grpc.aio.insecure_channel(endpoint)
    root = ca_file.read_bytes() if ca_file else None
    cert = client_cert_file.read_bytes() if client_cert_file else None
    key = client_key_file.read_bytes() if client_key_file else None
    credentials = grpc.ssl_channel_credentials(
        root_certificates=root,
        private_key=key,
        certificate_chain=cert,
    )
    return grpc.aio.secure_channel(endpoint, credentials)


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = int(round((len(ordered) - 1) * percentile))
    return ordered[max(0, min(index, len(ordered) - 1))]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run TraceForge privacy microbenchmarks or OTLP gateway load/soak tests"
    )
    subparsers = parser.add_subparsers(dest="mode", required=True)

    privacy = subparsers.add_parser("privacy", help="Benchmark in-process privacy scrubbing")
    privacy.add_argument("--iterations", type=int, default=1000)
    privacy.add_argument("--output", type=Path)

    gateway = subparsers.add_parser("gateway", help="Load or soak-test a running OTLP gateway")
    gateway.add_argument("--endpoint", required=True)
    gateway.add_argument("--requests", type=int, default=1000)
    gateway.add_argument("--concurrency", type=int, default=16)
    gateway.add_argument("--duration-seconds", type=float, default=0.0)
    gateway.add_argument("--timeout-seconds", type=float, default=10.0)
    gateway.add_argument("--insecure", action="store_true")
    gateway.add_argument("--ca-file", type=Path)
    gateway.add_argument("--client-cert-file", type=Path)
    gateway.add_argument("--client-key-file", type=Path)
    gateway.add_argument("--output", type=Path)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.mode == "privacy":
        result = run_privacy_benchmark(args.iterations)
        exit_code = 1 if result["privacy_leaks"] else 0
    else:
        result = asyncio.run(
            run_gateway_benchmark(
                args.endpoint,
                requests=args.requests,
                concurrency=args.concurrency,
                duration_seconds=args.duration_seconds,
                timeout_seconds=args.timeout_seconds,
                insecure=args.insecure,
                ca_file=args.ca_file,
                client_cert_file=args.client_cert_file,
                client_key_file=args.client_key_file,
            )
        )
        exit_code = 0

    rendered = json.dumps(result, indent=2, sort_keys=True)
    print(rendered)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

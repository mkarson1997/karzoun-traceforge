from __future__ import annotations

import asyncio

import grpc
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)
from opentelemetry.proto.collector.trace.v1.trace_service_pb2_grpc import (
    TraceServiceServicer,
    TraceServiceStub,
    add_TraceServiceServicer_to_server,
)

from traceforge.gateway.server import PrivacyGateway
from traceforge.privacy import Scrubber


class FailingUpstream(TraceServiceServicer):
    async def Export(self, request, context):  # noqa: N802, ARG002
        await context.abort(grpc.StatusCode.UNAVAILABLE, "injected upstream outage")


def test_gateway_fails_closed_when_upstream_is_unavailable() -> None:
    asyncio.run(_exercise_upstream_failure())


async def _exercise_upstream_failure() -> None:
    upstream = grpc.aio.server()
    add_TraceServiceServicer_to_server(FailingUpstream(), upstream)
    upstream_port = upstream.add_insecure_port("127.0.0.1:0")
    await upstream.start()

    upstream_channel = grpc.aio.insecure_channel(f"127.0.0.1:{upstream_port}")
    servicer = PrivacyGateway(
        scrubber=Scrubber(),
        upstream_stub=TraceServiceStub(upstream_channel),
        export_timeout_seconds=2,
    )
    gateway = grpc.aio.server()
    add_TraceServiceServicer_to_server(servicer, gateway)
    gateway_port = gateway.add_insecure_port("127.0.0.1:0")
    await gateway.start()

    client_channel = grpc.aio.insecure_channel(f"127.0.0.1:{gateway_port}")
    request = ExportTraceServiceRequest()
    request.resource_spans.add().scope_spans.add().spans.add().name = "failure injection"

    try:
        try:
            await TraceServiceStub(client_channel).Export(request, timeout=3)
        except grpc.aio.AioRpcError as exc:
            assert exc.code() == grpc.StatusCode.UNAVAILABLE
            assert "upstream OTLP export failed" in exc.details()
        else:
            raise AssertionError("gateway should fail closed when upstream is unavailable")

        metrics = servicer.metrics_snapshot()
        assert metrics["accepted_exports_total"] == 1
        assert metrics["upstream_failures_total"] == 1
        assert metrics["inflight_exports"] == 0
    finally:
        await client_channel.close()
        await gateway.stop(grace=0)
        await upstream_channel.close()
        await upstream.stop(grace=0)

import asyncio

import grpc
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)
from opentelemetry.proto.collector.trace.v1.trace_service_pb2_grpc import (
    TraceServiceServicer,
    TraceServiceStub,
    add_TraceServiceServicer_to_server,
)

from traceforge.gateway.server import PrivacyGateway
from traceforge.privacy import Scrubber


class CaptureService(TraceServiceServicer):
    def __init__(self) -> None:
        self.last_request = None

    async def Export(self, request, context):  # noqa: N802
        captured = ExportTraceServiceRequest()
        captured.CopyFrom(request)
        self.last_request = captured
        return ExportTraceServiceResponse()


def test_gateway_forwards_only_sanitized_request() -> None:
    asyncio.run(_exercise_gateway())


async def _exercise_gateway() -> None:
    upstream = grpc.aio.server()
    capture = CaptureService()
    add_TraceServiceServicer_to_server(capture, upstream)
    upstream_port = upstream.add_insecure_port("127.0.0.1:0")
    await upstream.start()

    upstream_channel = grpc.aio.insecure_channel(f"127.0.0.1:{upstream_port}")
    gateway = grpc.aio.server()
    add_TraceServiceServicer_to_server(
        PrivacyGateway(
            scrubber=Scrubber(),
            upstream_stub=TraceServiceStub(upstream_channel),
            export_timeout_seconds=5,
        ),
        gateway,
    )
    gateway_port = gateway.add_insecure_port("127.0.0.1:0")
    await gateway.start()

    client_channel = grpc.aio.insecure_channel(f"127.0.0.1:{gateway_port}")
    try:
        request = ExportTraceServiceRequest()
        span = request.resource_spans.add().scope_spans.add().spans.add()
        span.name = "agent run dev@example.com"
        kv = span.attributes.add()
        kv.key = "gen_ai.prompt"
        kv.value.string_value = "do not persist this"
        safe = span.attributes.add()
        safe.key = "service.name"
        safe.value.string_value = "traceforge-test"

        await TraceServiceStub(client_channel).Export(request, timeout=5)

        assert capture.last_request is not None
        forwarded_span = capture.last_request.resource_spans[0].scope_spans[0].spans[0]
        assert "dev@example.com" not in forwarded_span.name
        forwarded = {item.key: item.value.string_value for item in forwarded_span.attributes}
        assert "gen_ai.prompt" not in forwarded
        assert forwarded["service.name"] == "traceforge-test"
    finally:
        await client_channel.close()
        await gateway.stop(grace=0)
        await upstream_channel.close()
        await upstream.stop(grace=0)

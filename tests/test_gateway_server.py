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


class BlockingUpstreamStub:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def Export(self, request, timeout=None):  # noqa: N802, ARG002
        self.entered.set()
        await self.release.wait()
        return ExportTraceServiceResponse()


class ImmediateUpstreamStub:
    def __init__(self) -> None:
        self.calls = 0

    async def Export(self, request, timeout=None):  # noqa: N802, ARG002
        self.calls += 1
        return ExportTraceServiceResponse()


class AbortCalled(Exception):
    def __init__(self, code: grpc.StatusCode, details: str) -> None:
        super().__init__(details)
        self.code = code
        self.details = details


class FakeContext:
    def __init__(self, metadata=()) -> None:
        self._metadata = metadata

    def invocation_metadata(self):
        return self._metadata

    async def abort(self, code: grpc.StatusCode, details: str):
        raise AbortCalled(code, details)


def test_gateway_forwards_only_sanitized_request() -> None:
    asyncio.run(_exercise_gateway())


def test_gateway_rejects_when_admission_capacity_is_exhausted() -> None:
    asyncio.run(_exercise_backpressure())


def test_gateway_enforces_forwarded_client_certificate_allowlist() -> None:
    asyncio.run(_exercise_client_certificate_authorization())


async def _exercise_gateway() -> None:
    upstream = grpc.aio.server()
    capture = CaptureService()
    add_TraceServiceServicer_to_server(capture, upstream)
    upstream_port = upstream.add_insecure_port("127.0.0.1:0")
    await upstream.start()

    upstream_channel = grpc.aio.insecure_channel(f"127.0.0.1:{upstream_port}")
    gateway = grpc.aio.server()
    servicer = PrivacyGateway(
        scrubber=Scrubber(),
        upstream_stub=TraceServiceStub(upstream_channel),
        export_timeout_seconds=5,
    )
    add_TraceServiceServicer_to_server(servicer, gateway)
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

        metrics = servicer.metrics_snapshot()
        assert metrics["accepted_exports_total"] == 1
        assert metrics["rejected_exports_total"] == 0
        assert metrics["inflight_exports"] == 0
        assert "traceforge_gateway_exports_accepted_total 1" in servicer.render_metrics()
    finally:
        await client_channel.close()
        await gateway.stop(grace=0)
        await upstream_channel.close()
        await upstream.stop(grace=0)


async def _exercise_backpressure() -> None:
    upstream = BlockingUpstreamStub()
    servicer = PrivacyGateway(
        scrubber=Scrubber(),
        upstream_stub=upstream,  # type: ignore[arg-type]
        export_timeout_seconds=5,
        max_inflight_exports=1,
        admission_timeout_seconds=0.01,
    )
    request = ExportTraceServiceRequest()
    request.resource_spans.add().scope_spans.add().spans.add().name = "blocking export"

    first = asyncio.create_task(servicer.Export(request, FakeContext()))
    await asyncio.wait_for(upstream.entered.wait(), timeout=1)
    assert servicer.metrics_snapshot()["inflight_exports"] == 1

    try:
        await servicer.Export(request, FakeContext())
    except AbortCalled as exc:
        assert exc.code == grpc.StatusCode.RESOURCE_EXHAUSTED
        assert "retry with backoff" in exc.details
    else:
        raise AssertionError("second export should have been rejected")

    metrics = servicer.metrics_snapshot()
    assert metrics["accepted_exports_total"] == 1
    assert metrics["rejected_exports_total"] == 1
    assert metrics["inflight_exports"] == 1
    assert "traceforge_gateway_backpressure_active 1" in servicer.render_metrics()

    upstream.release.set()
    await first
    assert servicer.metrics_snapshot()["inflight_exports"] == 0


async def _exercise_client_certificate_authorization() -> None:
    trusted_hash = "AB" * 32
    upstream = ImmediateUpstreamStub()
    servicer = PrivacyGateway(
        scrubber=Scrubber(),
        upstream_stub=upstream,  # type: ignore[arg-type]
        export_timeout_seconds=5,
        trusted_client_certificate_hashes=frozenset({trusted_hash}),
    )
    request = ExportTraceServiceRequest()
    request.resource_spans.add().scope_spans.add().spans.add().name = "authenticated export"

    try:
        await servicer.Export(request, FakeContext())
    except AbortCalled as exc:
        assert exc.code == grpc.StatusCode.UNAUTHENTICATED
    else:
        raise AssertionError("missing client certificate should have been rejected")

    wrong_metadata = [("x-forwarded-client-cert", f"Hash={'CD' * 32}")]
    try:
        await servicer.Export(request, FakeContext(wrong_metadata))
    except AbortCalled as exc:
        assert exc.code == grpc.StatusCode.UNAUTHENTICATED
    else:
        raise AssertionError("untrusted client certificate should have been rejected")

    trusted_metadata = [("x-forwarded-client-cert", f"Hash={trusted_hash}")]
    await servicer.Export(request, FakeContext(trusted_metadata))

    metrics = servicer.metrics_snapshot()
    assert upstream.calls == 1
    assert metrics["accepted_exports_total"] == 1
    assert metrics["client_auth_rejections_total"] == 2
    assert "traceforge_gateway_client_auth_rejections_total 2" in servicer.render_metrics()

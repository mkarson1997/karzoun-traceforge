from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from pathlib import Path
from uuid import uuid4

import grpc
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceResponse,
)
from opentelemetry.proto.collector.trace.v1.trace_service_pb2_grpc import (
    TraceServiceServicer,
    TraceServiceStub,
    add_TraceServiceServicer_to_server,
)

from traceforge.gateway.config import GatewayConfig
from traceforge.gateway.otel_scrub import attach_privacy_summary, scrub_trace_export_request
from traceforge.privacy import Scrubber

LOGGER = logging.getLogger("traceforge.gateway")


class PrivacyGateway(TraceServiceServicer):
    def __init__(
        self,
        *,
        scrubber: Scrubber,
        upstream_stub: TraceServiceStub,
        export_timeout_seconds: float,
    ) -> None:
        self._scrubber = scrubber
        self._upstream_stub = upstream_stub
        self._export_timeout_seconds = export_timeout_seconds
        self.ready = True

    async def Export(self, request, context):  # noqa: N802 - gRPC generated method name
        sanitized = type(request)()
        sanitized.CopyFrom(request)
        stats = scrub_trace_export_request(sanitized, self._scrubber)
        export_id = uuid4().hex
        attach_privacy_summary(sanitized, stats, export_id=export_id)

        LOGGER.info(
            "trace export scrubbed export=%s spans=%d events=%d links=%d findings=%d "
            "removed=%d rewritten=%d",
            export_id,
            stats.spans_seen,
            stats.events_seen,
            stats.links_seen,
            stats.findings,
            stats.attributes_removed,
            stats.attributes_rewritten,
        )
        try:
            return await self._upstream_stub.Export(
                sanitized,
                timeout=self._export_timeout_seconds,
            )
        except grpc.aio.AioRpcError as exc:
            LOGGER.warning("upstream OTLP export failed: %s", exc.code())
            await context.abort(grpc.StatusCode.UNAVAILABLE, "upstream OTLP export failed")
        return ExportTraceServiceResponse()


async def serve(config: GatewayConfig) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    scrubber = Scrubber(
        config.privacy_policy(),
        tokenization_key=config.tokenization_key,
    )
    channel = _create_upstream_channel(config)
    stub = TraceServiceStub(channel)
    servicer = PrivacyGateway(
        scrubber=scrubber,
        upstream_stub=stub,
        export_timeout_seconds=config.export_timeout_seconds,
    )

    server = grpc.aio.server(
        options=[
            (
                "grpc.max_receive_message_length",
                config.max_receive_message_mib * 1024 * 1024,
            )
        ]
    )
    add_TraceServiceServicer_to_server(servicer, server)
    server.add_insecure_port(config.listen_address)

    health_server = await asyncio.start_server(
        lambda reader, writer: _handle_health(reader, writer, servicer),
        *_split_host_port(config.health_address),
    )

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop_event.set)

    await server.start()
    LOGGER.info(
        "TraceForge privacy gateway listening on %s; upstream=%s",
        config.listen_address,
        config.upstream_endpoint,
    )

    try:
        await stop_event.wait()
    finally:
        servicer.ready = False
        health_server.close()
        await health_server.wait_closed()
        await server.stop(grace=5)
        await channel.close()


def _create_upstream_channel(config: GatewayConfig) -> grpc.aio.Channel:
    if config.upstream_insecure:
        return grpc.aio.insecure_channel(config.upstream_endpoint)

    root = _read_optional(config.upstream_ca_file)
    cert = _read_optional(config.upstream_client_cert_file)
    key = _read_optional(config.upstream_client_key_file)
    if bool(cert) != bool(key):
        raise ValueError("client certificate and key must be configured together")
    credentials = grpc.ssl_channel_credentials(
        root_certificates=root,
        private_key=key,
        certificate_chain=cert,
    )
    return grpc.aio.secure_channel(config.upstream_endpoint, credentials)


def _read_optional(path: Path | None) -> bytes | None:
    return path.read_bytes() if path else None


def _split_host_port(value: str) -> tuple[str, int]:
    host, port_text = value.rsplit(":", 1)
    return host, int(port_text)


async def _handle_health(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    servicer: PrivacyGateway,
) -> None:
    try:
        line = await asyncio.wait_for(reader.readline(), timeout=2)
        parts = line.decode("ascii", errors="ignore").split()
        path = parts[1] if len(parts) >= 2 else "/"
        ready = servicer.ready
        if path == "/healthz":
            code, reason, body = 200, "OK", "ok\n"
        elif path == "/readyz" and ready:
            code, reason, body = 200, "OK", "ready\n"
        elif path == "/readyz":
            code, reason, body = 503, "Service Unavailable", "not ready\n"
        else:
            code, reason, body = 404, "Not Found", "not found\n"
        payload = body.encode("utf-8")
        writer.write(
            (
                f"HTTP/1.1 {code} {reason}\r\n"
                "Content-Type: text/plain; charset=utf-8\r\n"
                f"Content-Length: {len(payload)}\r\n"
                "Connection: close\r\n\r\n"
            ).encode("ascii")
            + payload
        )
        await writer.drain()
    except (TimeoutError, ConnectionError):
        pass
    finally:
        writer.close()
        await writer.wait_closed()


def main() -> int:
    asyncio.run(serve(GatewayConfig.from_env()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

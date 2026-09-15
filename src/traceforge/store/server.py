from __future__ import annotations

import argparse
import json
import logging
import os
from concurrent import futures
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import grpc
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceResponse,
)
from opentelemetry.proto.collector.trace.v1.trace_service_pb2_grpc import (
    TraceServiceServicer,
    add_TraceServiceServicer_to_server,
)

from traceforge.gateway.otel_scrub import scrub_trace_export_request
from traceforge.privacy import Scrubber
from traceforge.store.repository import TraceRepository
from traceforge.store.viewer import VIEWER_HTML

LOGGER = logging.getLogger("traceforge.store")


class TraceStoreService(TraceServiceServicer):
    def __init__(self, repository: TraceRepository) -> None:
        self._repository = repository
        self._scrubber = Scrubber()

    def Export(self, request, context):  # noqa: N802 - generated gRPC method name
        sanitized = type(request)()
        sanitized.CopyFrom(request)
        stats = scrub_trace_export_request(sanitized, self._scrubber)
        count = self._repository.ingest(sanitized)
        LOGGER.info(
            "stored trace export spans=%d defense_findings=%d persisted=%d",
            stats.spans_seen,
            stats.findings,
            count,
        )
        return ExportTraceServiceResponse()


class ViewerServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], repository: TraceRepository) -> None:
        super().__init__(address, ViewerHandler)
        self.repository = repository


class ViewerHandler(BaseHTTPRequestHandler):
    server: ViewerServer

    def log_message(self, format: str, *args: Any) -> None:
        LOGGER.debug("viewer %s", format % args)

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler method name
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path == "/":
            self._send_html(VIEWER_HTML)
            return
        if path == "/healthz":
            self._send_json({"status": "ok"})
            return
        if path == "/api/stats":
            self._send_json(self.server.repository.stats())
            return
        if path == "/api/sessions":
            self._send_json(self.server.repository.list_sessions(_int_param(query, "limit", 50)))
            return
        if path == "/api/privacy":
            self._send_json(
                self.server.repository.list_privacy_exports(_int_param(query, "limit", 50))
            )
            return
        if path == "/api/search":
            status_value = _optional_int_param(query, "status")
            self._send_json(
                self.server.repository.search_spans(
                    _text_param(query, "q"),
                    service=_optional_text_param(query, "service"),
                    agent=_optional_text_param(query, "agent"),
                    status_code=status_value,
                    limit=_int_param(query, "limit", 100),
                )
            )
            return
        if path == "/api/export.jsonl":
            rows = self.server.repository.export_spans(
                session_id=_optional_text_param(query, "session"),
                task_id=_optional_text_param(query, "task"),
                limit=_int_param(query, "limit", 10_000),
            )
            self._send_jsonl(rows, filename="traceforge-sanitized-spans.jsonl")
            return
        if path.startswith("/api/sessions/") and path.endswith("/traces"):
            encoded = path[len("/api/sessions/") : -len("/traces")].strip("/")
            self._send_json(self.server.repository.list_traces_for_session(unquote(encoded)))
            return
        if path.startswith("/api/traces/"):
            trace_id = unquote(path.removeprefix("/api/traces/")).strip("/")
            trace = self.server.repository.get_trace(trace_id)
            if trace is None:
                self._send_json({"error": "trace not found"}, status=HTTPStatus.NOT_FOUND)
            else:
                self._send_json(trace)
            return
        self._send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)

    def _send_json(self, value: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self._security_headers()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_jsonl(self, rows: list[dict[str, Any]], *, filename: str) -> None:
        body = b"".join(
            json.dumps(row, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n"
            for row in rows
        )
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self._security_headers()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, value: str) -> None:
        body = value.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self._security_headers()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
            "connect-src 'self'; img-src 'self'; frame-ancestors 'none'; base-uri 'none'",
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the TraceForge local trace store and viewer")
    parser.add_argument(
        "--db",
        default=os.getenv("TRACEFORGE_STORE_DB", "/var/lib/traceforge/traceforge.db"),
    )
    parser.add_argument(
        "--otlp-address",
        default=os.getenv("TRACEFORGE_STORE_OTLP_ADDRESS", "0.0.0.0:4320"),
    )
    parser.add_argument(
        "--http-address",
        default=os.getenv("TRACEFORGE_STORE_HTTP_ADDRESS", "0.0.0.0:8081"),
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    repository = TraceRepository(Path(args.db))
    grpc_server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=8),
        options=[("grpc.max_receive_message_length", 16 * 1024 * 1024)],
    )
    add_TraceServiceServicer_to_server(TraceStoreService(repository), grpc_server)
    grpc_server.add_insecure_port(args.otlp_address)

    viewer = ViewerServer(_split_host_port(args.http_address), repository)
    viewer_thread = Thread(target=viewer.serve_forever, name="traceforge-viewer", daemon=True)

    grpc_server.start()
    viewer_thread.start()
    LOGGER.info("trace store OTLP=%s viewer=http://%s", args.otlp_address, args.http_address)
    try:
        grpc_server.wait_for_termination()
    except KeyboardInterrupt:
        LOGGER.info("shutdown requested")
    finally:
        viewer.shutdown()
        viewer.server_close()
        grpc_server.stop(grace=5).wait()
    return 0


def _split_host_port(value: str) -> tuple[str, int]:
    host, port = value.rsplit(":", 1)
    return host, int(port)


def _text_param(query: dict[str, list[str]], name: str, default: str = "") -> str:
    values = query.get(name)
    return values[0].strip() if values else default


def _optional_text_param(query: dict[str, list[str]], name: str) -> str | None:
    value = _text_param(query, name)
    return value or None


def _int_param(query: dict[str, list[str]], name: str, default: int) -> int:
    value = _text_param(query, name)
    try:
        return int(value) if value else default
    except ValueError:
        return default


def _optional_int_param(query: dict[str, list[str]], name: str) -> int | None:
    value = _text_param(query, name)
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


if __name__ == "__main__":
    raise SystemExit(main())

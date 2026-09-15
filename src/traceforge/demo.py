from __future__ import annotations

import argparse
import secrets
import time
import uuid
from dataclasses import dataclass

import grpc
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2_grpc import TraceServiceStub
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue
from opentelemetry.proto.trace.v1.trace_pb2 import Span, Status


@dataclass(frozen=True)
class DemoIdentity:
    trace_id: str
    session_id: str
    task_id: str


def build_demo_request(
    *,
    trace_id: bytes | None = None,
    session_id: str | None = None,
    task_id: str | None = None,
    start_ns: int | None = None,
) -> tuple[ExportTraceServiceRequest, DemoIdentity]:
    trace_bytes = trace_id or secrets.token_bytes(16)
    if len(trace_bytes) != 16:
        raise ValueError("trace_id must be 16 bytes")
    session = session_id or f"session-{uuid.uuid4().hex[:10]}"
    task = task_id or f"task-{uuid.uuid4().hex[:10]}"
    base = start_ns or time.time_ns()

    request = ExportTraceServiceRequest()
    resource_spans = request.resource_spans.add()
    resource_spans.resource.attributes.extend(
        [
            _kv("service.name", "traceforge-demo-agent"),
            _kv("service.version", "0.1.0"),
            _kv("deployment.environment", "local-demo"),
        ]
    )
    scope_spans = resource_spans.scope_spans.add()
    scope_spans.scope.name = "traceforge.demo"
    scope_spans.scope.version = "0.1.0"

    root_id = b"\x01" * 8
    plan_id = b"\x02" * 8
    tool_id = b"\x03" * 8
    complete_id = b"\x04" * 8

    root = _span(
        trace_bytes,
        root_id,
        b"",
        "agent.task",
        base,
        620_000_000,
        [
            _kv("traceforge.session.id", session),
            _kv("traceforge.task.id", task),
            _kv("traceforge.agent.name", "demo-coding-agent"),
            _kv("gen_ai.system", "synthetic"),
            _kv("user.email", "developer@example.com"),
            _kv("agent.prompt", "Fix the release bug using token ghp_abcdefghijklmnopqrstuvwxyz123456"),
        ],
    )
    root.events.add(
        name="task.accepted",
        time_unix_nano=base + 10_000_000,
        attributes=[_kv("request_body", "customer secret body")],
    )

    plan = _span(
        trace_bytes,
        plan_id,
        root_id,
        "agent.plan",
        base + 50_000_000,
        120_000_000,
        [
            _kv("traceforge.session.id", session),
            _kv("traceforge.task.id", task),
            _kv("plan.steps", 3),
            _kv("model", "synthetic-planner"),
        ],
    )

    tool = _span(
        trace_bytes,
        tool_id,
        root_id,
        "tool.shell",
        base + 190_000_000,
        240_000_000,
        [
            _kv("traceforge.session.id", session),
            _kv("traceforge.task.id", task),
            _kv("tool.name", "shell"),
            _kv("tool.exit_code", 0),
            _kv("authorization", "Bearer abcdefghijklmnopqrstuvwxyz0123456789"),
            _kv("source.code", "print('sensitive source code')"),
        ],
    )

    complete = _span(
        trace_bytes,
        complete_id,
        root_id,
        "agent.complete",
        base + 460_000_000,
        130_000_000,
        [
            _kv("traceforge.session.id", session),
            _kv("traceforge.task.id", task),
            _kv("result", "success"),
            _kv("completion", "private generated completion"),
        ],
    )

    scope_spans.spans.extend([root, plan, tool, complete])
    identity = DemoIdentity(trace_id=trace_bytes.hex(), session_id=session, task_id=task)
    return request, identity


def send_demo(endpoint: str, timeout: float = 10.0) -> DemoIdentity:
    request, identity = build_demo_request()
    with grpc.insecure_channel(endpoint) as channel:
        TraceServiceStub(channel).Export(request, timeout=timeout)
    return identity


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Send synthetic coding-agent telemetry")
    parser.add_argument("--endpoint", default="127.0.0.1:4317")
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--interval", type=float, default=0.15)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    count = max(1, min(args.count, 100))
    for index in range(count):
        identity = send_demo(args.endpoint)
        print(
            f"sent session={identity.session_id} task={identity.task_id} trace={identity.trace_id}"
        )
        if index + 1 < count:
            time.sleep(max(0.0, args.interval))
    return 0


def _span(
    trace_id: bytes,
    span_id: bytes,
    parent_span_id: bytes,
    name: str,
    start_ns: int,
    duration_ns: int,
    attributes: list[KeyValue],
) -> Span:
    return Span(
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id=parent_span_id,
        name=name,
        kind=Span.SPAN_KIND_INTERNAL,
        start_time_unix_nano=start_ns,
        end_time_unix_nano=start_ns + duration_ns,
        attributes=attributes,
        status=Status(code=Status.STATUS_CODE_OK),
    )


def _kv(key: str, value: str | int | bool | float) -> KeyValue:
    any_value = AnyValue()
    if isinstance(value, bool):
        any_value.bool_value = value
    elif isinstance(value, int):
        any_value.int_value = value
    elif isinstance(value, float):
        any_value.double_value = value
    else:
        any_value.string_value = value
    return KeyValue(key=key, value=any_value)


if __name__ == "__main__":
    raise SystemExit(main())

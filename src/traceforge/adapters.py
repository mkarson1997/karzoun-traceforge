from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, TextIO

import grpc
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2_grpc import TraceServiceStub
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue
from opentelemetry.proto.trace.v1.trace_pb2 import Span, Status

from traceforge.policy import OrganizationPolicy, load_policy
from traceforge.privacy import Scrubber

Scalar = str | int | bool | float


@dataclass(frozen=True)
class AdapterEvent:
    source: str
    name: str
    timestamp_ns: int
    session_id: str | None = None
    task_id: str | None = None
    status: str | None = None
    attributes: Mapping[str, Scalar] = field(default_factory=dict)


class EventAdapter(Protocol):
    source: str

    def normalize(self, record: Mapping[str, Any]) -> AdapterEvent | None: ...


class CodexAdapter:
    source = "codex"

    def __init__(self) -> None:
        self._session_id: str | None = None

    def normalize(self, record: Mapping[str, Any]) -> AdapterEvent | None:
        event_type = _text(record.get("type"))
        if not event_type:
            return None
        if event_type == "thread.started":
            self._session_id = _text(record.get("thread_id")) or self._session_id

        item = record.get("item")
        item_map = item if isinstance(item, Mapping) else {}
        attributes: dict[str, Scalar] = {"adapter.vendor": "openai"}
        item_type = _text(item_map.get("type"))
        item_id = _text(item_map.get("id"))
        item_status = _text(item_map.get("status"))
        if item_type:
            attributes["adapter.item.type"] = item_type
        if item_id:
            attributes["adapter.item.id"] = item_id
        if item_status:
            attributes["adapter.item.status"] = item_status
        if item_type in {"command_execution", "mcp_tool_call", "web_search"}:
            attributes["tool.name"] = item_type

        usage = record.get("usage")
        if isinstance(usage, Mapping):
            _copy_int(usage, "input_tokens", attributes, "gen_ai.usage.input_tokens")
            _copy_int(
                usage,
                "cached_input_tokens",
                attributes,
                "gen_ai.usage.cached_input_tokens",
            )
            _copy_int(usage, "output_tokens", attributes, "gen_ai.usage.output_tokens")
            _copy_int(
                usage,
                "reasoning_output_tokens",
                attributes,
                "gen_ai.usage.reasoning_tokens",
            )

        status = item_status
        if event_type == "turn.completed":
            status = "completed"
        elif event_type in {"turn.failed", "error"}:
            status = "failed"

        return AdapterEvent(
            source=self.source,
            name=f"codex.{event_type}",
            timestamp_ns=_timestamp_ns(record.get("timestamp")),
            session_id=self._session_id,
            task_id=_text(record.get("turn_id")),
            status=status,
            attributes=attributes,
        )


class ClaudeCodeAdapter:
    source = "claude-code"

    def __init__(self) -> None:
        self._session_id: str | None = None

    def normalize(self, record: Mapping[str, Any]) -> AdapterEvent | None:
        message_type = _text(record.get("type"))
        if not message_type:
            return None
        session = _text(record.get("session_id"))
        if session:
            self._session_id = session

        subtype = _text(record.get("subtype"))
        event_name = f"claude.{message_type}"
        if subtype:
            event_name += f".{subtype}"

        attributes: dict[str, Scalar] = {"adapter.vendor": "anthropic"}
        status: str | None = None

        if message_type == "system" and subtype == "init":
            model = _text(record.get("model"))
            permission_mode = _text(record.get("permissionMode"))
            if model:
                attributes["gen_ai.request.model"] = model
            if permission_mode:
                attributes["adapter.permission_mode"] = permission_mode

        message = record.get("message")
        if isinstance(message, Mapping):
            model = _text(message.get("model"))
            if model:
                attributes["gen_ai.response.model"] = model
            usage = message.get("usage")
            if isinstance(usage, Mapping):
                _copy_int(
                    usage,
                    "input_tokens",
                    attributes,
                    "gen_ai.usage.input_tokens",
                )
                _copy_int(
                    usage,
                    "output_tokens",
                    attributes,
                    "gen_ai.usage.output_tokens",
                )
                _copy_int(
                    usage,
                    "cache_read_input_tokens",
                    attributes,
                    "gen_ai.usage.cached_input_tokens",
                )

            content = message.get("content")
            if isinstance(content, list):
                block_types: list[str] = []
                tool_names: list[str] = []
                for block in content:
                    if not isinstance(block, Mapping):
                        continue
                    block_type = _text(block.get("type"))
                    if block_type:
                        block_types.append(block_type)
                    if block_type == "tool_use":
                        tool_name = _text(block.get("name"))
                        if tool_name:
                            tool_names.append(tool_name)
                if block_types:
                    attributes["adapter.content.types"] = ",".join(block_types)
                if tool_names:
                    attributes["tool.names"] = ",".join(tool_names)
                    attributes["tool.count"] = len(tool_names)

        if message_type == "result":
            _copy_int(record, "num_turns", attributes, "adapter.turns")
            _copy_number(record, "duration_ms", attributes, "adapter.duration_ms")
            _copy_number(
                record,
                "total_cost_usd",
                attributes,
                "gen_ai.usage.cost_usd",
            )
            is_error = record.get("is_error")
            if isinstance(is_error, bool):
                attributes["adapter.is_error"] = is_error
                status = "failed" if is_error else "completed"
            elif subtype and subtype.startswith("error"):
                status = "failed"
            else:
                status = "completed"

        return AdapterEvent(
            source=self.source,
            name=event_name,
            timestamp_ns=_timestamp_ns(record.get("timestamp")),
            session_id=self._session_id,
            task_id=_text(record.get("request_id")),
            status=status,
            attributes=attributes,
        )


class CopilotAdapter:
    source = "github-copilot"

    def normalize(self, record: Mapping[str, Any]) -> AdapterEvent | None:
        event_name = _first_text(
            record,
            "hook_event_name",
            "hookEventName",
            "eventName",
            "event",
            "type",
        )
        if not event_name:
            return None

        attributes: dict[str, Scalar] = {"adapter.vendor": "github"}
        tool_name = _first_text(record, "tool_name", "toolName")
        if tool_name:
            attributes["tool.name"] = tool_name
        session_source = _first_text(record, "source")
        if session_source:
            attributes["adapter.session_source"] = session_source

        return AdapterEvent(
            source=self.source,
            name=f"copilot.{event_name}",
            timestamp_ns=_timestamp_ns(record.get("timestamp")),
            session_id=_first_text(record, "session_id", "sessionId"),
            task_id=_first_text(record, "request_id", "requestId"),
            status=_first_text(record, "status", "outcome"),
            attributes=attributes,
        )


class GenericAdapter:
    source = "generic"

    def normalize(self, record: Mapping[str, Any]) -> AdapterEvent | None:
        name = _first_text(record, "name", "event", "type")
        if not name:
            return None

        attributes: dict[str, Scalar] = {"adapter.vendor": "generic"}
        supplied = record.get("attributes")
        if isinstance(supplied, Mapping):
            for key, value in supplied.items():
                candidate = _scalar(value)
                if candidate is not None:
                    attributes[str(key)] = candidate
                elif value is not None:
                    attributes[str(key)] = _compact_json(value)

        for key, target in (
            ("model", "gen_ai.request.model"),
            ("tool_name", "tool.name"),
            ("service", "service.name"),
            ("agent", "traceforge.agent.name"),
        ):
            value = _scalar(record.get(key))
            if value is not None:
                attributes[target] = value

        return AdapterEvent(
            source=self.source,
            name=f"generic.{name}",
            timestamp_ns=_timestamp_ns(record.get("timestamp")),
            session_id=_first_text(record, "session_id", "sessionId"),
            task_id=_first_text(record, "task_id", "taskId"),
            status=_first_text(record, "status"),
            attributes=attributes,
        )


def create_adapter(name: str) -> EventAdapter:
    normalized = name.strip().lower()
    factories = {
        "codex": CodexAdapter,
        "claude": ClaudeCodeAdapter,
        "copilot": CopilotAdapter,
        "generic": GenericAdapter,
    }
    try:
        return factories[normalized]()
    except KeyError as exc:
        raise ValueError(f"unsupported adapter: {name}") from exc


def normalize_records(
    name: str,
    records: list[Mapping[str, Any]],
) -> list[AdapterEvent]:
    adapter = create_adapter(name)
    result: list[AdapterEvent] = []
    for record in records:
        event = adapter.normalize(record)
        if event is not None:
            result.append(event)
    return result


def build_export_request(
    events: list[AdapterEvent],
    policy: OrganizationPolicy | None = None,
) -> ExportTraceServiceRequest:
    request = ExportTraceServiceRequest()
    if not events:
        return request

    resource_spans = request.resource_spans.add()
    sources = sorted({event.source for event in events})
    resource_spans.resource.attributes.extend(
        [
            _kv("service.name", "traceforge-agent-adapter"),
            _kv("service.version", "0.1.0"),
            _kv("traceforge.adapter.sources", ",".join(sources)),
        ]
    )
    if policy is not None:
        resource_spans.resource.attributes.extend(
            [_kv(key, value) for key, value in policy.metadata().items()]
        )
    scope_spans = resource_spans.scope_spans.add()
    scope_spans.scope.name = "traceforge.adapters"
    scope_spans.scope.version = "0.1.0"

    scrubber = Scrubber()
    for index, event in enumerate(events):
        session = event.session_id or "unassigned"
        task = event.task_id or "unassigned"
        trace_seed = f"{event.source}:{session}:{task}".encode()
        span_seed = (
            f"{event.source}:{session}:{task}:{index}:{event.name}:{event.timestamp_ns}"
        ).encode()
        trace_id = hashlib.blake2b(trace_seed, digest_size=16).digest()
        span_id = hashlib.blake2b(span_seed, digest_size=8).digest()

        attributes: dict[str, Any] = {
            "traceforge.agent.name": event.source,
            "traceforge.adapter.event": event.name,
        }
        if event.session_id:
            attributes["traceforge.session.id"] = event.session_id
        if event.task_id:
            attributes["traceforge.task.id"] = event.task_id
        event_attributes = dict(event.attributes)
        if policy is not None:
            policy.require_allowed(event.source)
            event_attributes = policy.filter_attributes(event_attributes)
        attributes.update(event_attributes)

        scrubbed = scrubber.scrub(attributes, path="$.adapter").value
        safe_attributes = _scalar_attributes(scrubbed)
        scope_spans.spans.append(
            Span(
                trace_id=trace_id,
                span_id=span_id,
                name=event.name,
                kind=Span.SPAN_KIND_INTERNAL,
                start_time_unix_nano=event.timestamp_ns,
                end_time_unix_nano=event.timestamp_ns + 1,
                attributes=[
                    _kv(key, value)
                    for key, value in safe_attributes.items()
                ],
                status=Status(code=_status_code(event.status)),
            )
        )
    return request


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Normalize Codex, Claude Code, Copilot, or generic JSON events "
            "and forward privacy-scrubbed OTLP traces"
        )
    )
    parser.add_argument(
        "adapter",
        choices=("codex", "claude", "copilot", "generic"),
    )
    parser.add_argument("--input", default="-", help="JSONL/JSON path, or - for stdin")
    parser.add_argument("--endpoint", default="127.0.0.1:4317")
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--timeout-seconds", type=float, default=10.0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--insecure", action="store_true")
    parser.add_argument("--ca-file", type=Path)
    parser.add_argument("--client-cert-file", type=Path)
    parser.add_argument("--client-key-file", type=Path)
    parser.add_argument("--policy-file", type=Path)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if not 1 <= args.batch_size <= 1000:
        raise SystemExit("--batch-size must be between 1 and 1000")
    if bool(args.client_cert_file) != bool(args.client_key_file):
        raise SystemExit(
            "--client-cert-file and --client-key-file must be provided together"
        )

    policy: OrganizationPolicy | None = None
    if args.policy_file is not None:
        try:
            policy = load_policy(args.policy_file)
            policy.require_allowed(args.adapter)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"traceforge-adapter: policy error: {exc}", file=sys.stderr)
            return 2
        if args.batch_size > policy.max_batch_size:
            print(
                "traceforge-adapter: --batch-size exceeds organization policy limit "
                f"({policy.max_batch_size})",
                file=sys.stderr,
            )
            return 2

    adapter = create_adapter(args.adapter)
    normalized = 0
    batches = 0
    buffer: list[AdapterEvent] = []
    channel = None
    stub = None

    if not args.dry_run:
        channel = _channel(
            args.endpoint,
            insecure=args.insecure,
            ca_file=args.ca_file,
            client_cert_file=args.client_cert_file,
            client_key_file=args.client_key_file,
        )
        stub = TraceServiceStub(channel)

    try:
        for record in _records(args.input):
            event = adapter.normalize(record)
            if event is None:
                continue
            normalized += 1
            buffer.append(event)
            if len(buffer) >= args.batch_size:
                _flush(buffer, stub, args.timeout_seconds, policy)
                batches += 1
                buffer.clear()
        if buffer:
            _flush(buffer, stub, args.timeout_seconds, policy)
            batches += 1
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"traceforge-adapter: {exc}", file=sys.stderr)
        return 2
    except grpc.RpcError as exc:
        print(
            f"traceforge-adapter: OTLP export failed: {exc.code().name}",
            file=sys.stderr,
        )
        return 3
    finally:
        if channel is not None:
            channel.close()

    print(
        json.dumps(
            {
                "adapter": args.adapter,
                "events_normalized": normalized,
                "batches": batches,
                "dry_run": bool(args.dry_run),
            },
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0


def _flush(
    events: list[AdapterEvent],
    stub: TraceServiceStub | None,
    timeout_seconds: float,
    policy: OrganizationPolicy | None,
) -> None:
    request = build_export_request(events, policy=policy)
    if stub is not None:
        stub.Export(request, timeout=timeout_seconds)


def _records(source: str) -> Iterator[Mapping[str, Any]]:
    if source == "-":
        yield from _records_from_stream(sys.stdin)
        return

    path = Path(source)
    with path.open("r", encoding="utf-8") as handle:
        first = _first_non_whitespace(handle)
        handle.seek(0)
        if first == "[":
            value = json.load(handle)
            if not isinstance(value, list):
                raise ValueError("JSON input must be an array or JSON Lines stream")
            for item in value:
                if not isinstance(item, Mapping):
                    raise ValueError("each JSON array item must be an object")
                yield item
            return
        yield from _records_from_stream(handle)


def _records_from_stream(stream: TextIO) -> Iterator[Mapping[str, Any]]:
    for line_number, line in enumerate(stream, start=1):
        text = line.strip()
        if not text:
            continue
        value = json.loads(text)
        if not isinstance(value, Mapping):
            raise ValueError(f"line {line_number} is not a JSON object")
        yield value


def _first_non_whitespace(stream: TextIO) -> str:
    while True:
        value = stream.read(1)
        if not value:
            return ""
        if not value.isspace():
            return value


def _channel(
    endpoint: str,
    *,
    insecure: bool,
    ca_file: Path | None,
    client_cert_file: Path | None,
    client_key_file: Path | None,
) -> grpc.Channel:
    if insecure:
        return grpc.insecure_channel(endpoint)
    root = ca_file.read_bytes() if ca_file else None
    cert = client_cert_file.read_bytes() if client_cert_file else None
    key = client_key_file.read_bytes() if client_key_file else None
    credentials = grpc.ssl_channel_credentials(
        root_certificates=root,
        private_key=key,
        certificate_chain=cert,
    )
    return grpc.secure_channel(endpoint, credentials)


def _timestamp_ns(value: Any) -> int:
    if value is None or value == "":
        return time.time_ns()
    if isinstance(value, (int, float)):
        return _numeric_timestamp_ns(float(value))
    text = str(value).strip()
    try:
        return _numeric_timestamp_ns(float(text))
    except ValueError:
        candidate = text.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(candidate)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return int(parsed.timestamp() * 1_000_000_000)


def _numeric_timestamp_ns(value: float) -> int:
    absolute = abs(value)
    if absolute >= 1e17:
        return int(value)
    if absolute >= 1e14:
        return int(value * 1_000)
    if absolute >= 1e11:
        return int(value * 1_000_000)
    return int(value * 1_000_000_000)


def _scalar(value: Any) -> Scalar | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float, str)):
        if isinstance(value, str):
            text = value.strip()
            return text if text else None
        return value
    return None


def _text(value: Any) -> str | None:
    candidate = _scalar(value)
    return candidate if isinstance(candidate, str) else None


def _first_text(record: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        candidate = _text(record.get(key))
        if candidate:
            return candidate
    return None


def _copy_int(
    source: Mapping[str, Any],
    source_key: str,
    target: dict[str, Scalar],
    target_key: str,
) -> None:
    value = _scalar(source.get(source_key))
    if isinstance(value, int) and not isinstance(value, bool):
        target[target_key] = value


def _copy_number(
    source: Mapping[str, Any],
    source_key: str,
    target: dict[str, Scalar],
    target_key: str,
) -> None:
    value = _scalar(source.get(source_key))
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        target[target_key] = value


def _status_code(value: str | None) -> int:
    normalized = (value or "").strip().lower()
    if normalized in {"ok", "success", "succeeded", "completed", "complete"}:
        return Status.STATUS_CODE_OK
    if normalized in {"error", "failed", "failure", "cancelled", "canceled"}:
        return Status.STATUS_CODE_ERROR
    return Status.STATUS_CODE_UNSET


def _scalar_attributes(values: Any) -> dict[str, Scalar]:
    if not isinstance(values, Mapping):
        return {}
    result: dict[str, Scalar] = {}
    for key, value in values.items():
        candidate = _scalar(value)
        if candidate is not None:
            result[str(key)] = candidate
        elif value is not None:
            result[str(key)] = _compact_json(value)
    return result


def _compact_json(value: Any) -> str:
    return json.dumps(
        value,
        separators=(",", ":"),
        sort_keys=True,
        ensure_ascii=False,
    )


def _kv(key: str, value: Scalar) -> KeyValue:
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

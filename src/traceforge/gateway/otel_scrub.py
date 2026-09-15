from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue

from traceforge.privacy import Scrubber


@dataclass(frozen=True)
class OtlpScrubStats:
    findings: int = 0
    attributes_removed: int = 0
    attributes_rewritten: int = 0
    spans_seen: int = 0
    events_seen: int = 0
    links_seen: int = 0

    def merge(self, other: OtlpScrubStats) -> OtlpScrubStats:
        return OtlpScrubStats(
            findings=self.findings + other.findings,
            attributes_removed=self.attributes_removed + other.attributes_removed,
            attributes_rewritten=self.attributes_rewritten + other.attributes_rewritten,
            spans_seen=self.spans_seen + other.spans_seen,
            events_seen=self.events_seen + other.events_seen,
            links_seen=self.links_seen + other.links_seen,
        )


def scrub_trace_export_request(
    request: ExportTraceServiceRequest,
    scrubber: Scrubber,
) -> OtlpScrubStats:
    """Scrub an OTLP trace export request in place and return non-sensitive counters."""

    total = OtlpScrubStats()
    for resource_index, resource_spans in enumerate(request.resource_spans):
        total = total.merge(
            _scrub_key_values(
                resource_spans.resource.attributes,
                scrubber,
                path=f"$.resource_spans[{resource_index}].resource.attributes",
            )
        )

        for scope_index, scope_spans in enumerate(resource_spans.scope_spans):
            scope = scope_spans.scope
            if scope.name:
                scope.name, count = _scrub_text(
                    scope.name,
                    scrubber,
                    path=(
                        f"$.resource_spans[{resource_index}]"
                        f".scope_spans[{scope_index}].scope.name"
                    ),
                )
                total = total.merge(OtlpScrubStats(findings=count))
            if scope.version:
                scope.version, count = _scrub_text(
                    scope.version,
                    scrubber,
                    path=(
                        f"$.resource_spans[{resource_index}]"
                        f".scope_spans[{scope_index}].scope.version"
                    ),
                )
                total = total.merge(OtlpScrubStats(findings=count))
            if hasattr(scope, "attributes"):
                total = total.merge(
                    _scrub_key_values(
                        scope.attributes,
                        scrubber,
                        path=(
                            f"$.resource_spans[{resource_index}]"
                            f".scope_spans[{scope_index}].scope.attributes"
                        ),
                    )
                )

            for span_index, span in enumerate(scope_spans.spans):
                total = total.merge(OtlpScrubStats(spans_seen=1))
                span_path = (
                    f"$.resource_spans[{resource_index}]"
                    f".scope_spans[{scope_index}].spans[{span_index}]"
                )
                span.name, count = _scrub_text(
                    span.name, scrubber, path=f"{span_path}.name"
                )
                total = total.merge(OtlpScrubStats(findings=count))

                if span.trace_state:
                    span.trace_state, count = _scrub_text(
                        span.trace_state, scrubber, path=f"{span_path}.trace_state"
                    )
                    total = total.merge(OtlpScrubStats(findings=count))

                total = total.merge(
                    _scrub_key_values(
                        span.attributes,
                        scrubber,
                        path=f"{span_path}.attributes",
                    )
                )

                for event_index, event in enumerate(span.events):
                    total = total.merge(OtlpScrubStats(events_seen=1))
                    event_path = f"{span_path}.events[{event_index}]"
                    event.name, count = _scrub_text(
                        event.name, scrubber, path=f"{event_path}.name"
                    )
                    total = total.merge(OtlpScrubStats(findings=count))
                    total = total.merge(
                        _scrub_key_values(
                            event.attributes,
                            scrubber,
                            path=f"{event_path}.attributes",
                        )
                    )

                for link_index, link in enumerate(span.links):
                    total = total.merge(OtlpScrubStats(links_seen=1))
                    total = total.merge(
                        _scrub_key_values(
                            link.attributes,
                            scrubber,
                            path=f"{span_path}.links[{link_index}].attributes",
                        )
                    )

                if span.status.message:
                    span.status.message, count = _scrub_text(
                        span.status.message,
                        scrubber,
                        path=f"{span_path}.status.message",
                    )
                    total = total.merge(OtlpScrubStats(findings=count))

    return total


def _scrub_key_values(
    attributes: Any,
    scrubber: Scrubber,
    *,
    path: str,
) -> OtlpScrubStats:
    rewritten: list[KeyValue] = []
    findings = 0
    removed = 0
    changed = 0

    for index, item in enumerate(attributes):
        native = _any_value_to_native(item.value)
        result = scrubber.scrub({item.key: native}, path=f"{path}[{index}]")
        findings += len(result.findings)
        if item.key not in result.value:
            removed += 1
            continue

        scrubbed_native = result.value[item.key]
        candidate = KeyValue(key=item.key)
        _native_to_any_value(scrubbed_native, candidate.value)
        rewritten.append(candidate)
        if scrubbed_native != native:
            changed += 1

    del attributes[:]
    attributes.extend(rewritten)
    return OtlpScrubStats(
        findings=findings,
        attributes_removed=removed,
        attributes_rewritten=changed,
    )


def _scrub_text(value: str, scrubber: Scrubber, *, path: str) -> tuple[str, int]:
    result = scrubber.scrub(value, path=path)
    return str(result.value), len(result.findings)


def _any_value_to_native(value: AnyValue) -> Any:
    kind = value.WhichOneof("value")
    if kind is None:
        return None
    if kind == "string_value":
        return value.string_value
    if kind == "bool_value":
        return value.bool_value
    if kind == "int_value":
        return value.int_value
    if kind == "double_value":
        return value.double_value
    if kind == "bytes_value":
        # Binary telemetry attributes can contain opaque credentials. Preserve neither
        # raw bytes nor a reversible representation at the privacy boundary.
        return "[REDACTED:binary]" if value.bytes_value else ""
    if kind == "array_value":
        return [_any_value_to_native(item) for item in value.array_value.values]
    if kind == "kvlist_value":
        return {
            item.key: _any_value_to_native(item.value)
            for item in value.kvlist_value.values
        }
    raise ValueError(f"unsupported AnyValue kind: {kind}")


def _native_to_any_value(value: Any, target: AnyValue) -> None:
    target.Clear()
    if value is None:
        target.string_value = ""
    elif isinstance(value, bool):
        target.bool_value = value
    elif isinstance(value, int):
        target.int_value = value
    elif isinstance(value, float):
        target.double_value = value
    elif isinstance(value, str):
        target.string_value = value
    elif isinstance(value, dict):
        for key, item in value.items():
            kv = target.kvlist_value.values.add()
            kv.key = str(key)
            _native_to_any_value(item, kv.value)
    elif isinstance(value, (list, tuple)):
        for item in value:
            child = target.array_value.values.add()
            _native_to_any_value(item, child)
    else:
        target.string_value = str(value)

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)

from traceforge.gateway import scrub_trace_export_request
from traceforge.privacy import Scrubber


def _string_attribute(target, key: str, value: str) -> None:
    kv = target.add()
    kv.key = key
    kv.value.string_value = value


def test_otlp_request_scrubs_resource_span_event_and_link_attributes() -> None:
    request = ExportTraceServiceRequest()
    resource_spans = request.resource_spans.add()
    _string_attribute(resource_spans.resource.attributes, "user.email", "dev@example.com")
    _string_attribute(resource_spans.resource.attributes, "service.name", "demo")

    scope_spans = resource_spans.scope_spans.add()
    scope_spans.scope.name = "agent"
    span = scope_spans.spans.add()
    span.name = "run for dev@example.com"
    _string_attribute(span.attributes, "gen_ai.prompt", "raw sensitive prompt")
    _string_attribute(span.attributes, "safe", "Bearer abcdefghijklmnopqrstuvwxyz123456")

    event = span.events.add()
    event.name = "tool call dev@example.com"
    _string_attribute(event.attributes, "result", "owner=dev@example.com")

    link = span.links.add()
    _string_attribute(link.attributes, "source.content", "secret source")

    stats = scrub_trace_export_request(request, Scrubber())

    resource = {item.key: item.value.string_value for item in resource_spans.resource.attributes}
    attributes = {item.key: item.value.string_value for item in span.attributes}
    event_attributes = {item.key: item.value.string_value for item in event.attributes}

    assert "dev@example.com" not in resource["user.email"]
    assert resource["service.name"] == "demo"
    assert "dev@example.com" not in span.name
    assert "gen_ai.prompt" not in attributes
    assert "abcdefghijklmnopqrstuvwxyz123456" not in attributes["safe"]
    assert "dev@example.com" not in event.name
    assert "dev@example.com" not in event_attributes["result"]
    assert len(link.attributes) == 0
    assert stats.spans_seen == 1
    assert stats.events_seen == 1
    assert stats.links_seen == 1
    assert stats.attributes_removed == 2
    assert stats.findings >= 7


def test_binary_attribute_never_crosses_privacy_boundary_raw() -> None:
    request = ExportTraceServiceRequest()
    span = request.resource_spans.add().scope_spans.add().spans.add()
    kv = span.attributes.add()
    kv.key = "opaque"
    kv.value.bytes_value = b"raw-binary-secret"

    scrub_trace_export_request(request, Scrubber())

    value = span.attributes[0].value
    assert value.WhichOneof("value") == "string_value"
    assert value.string_value == "[REDACTED:binary]"

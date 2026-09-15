"""OTLP privacy gateway."""

from .otel_scrub import OtlpScrubStats, scrub_trace_export_request

__all__ = ["OtlpScrubStats", "scrub_trace_export_request"]

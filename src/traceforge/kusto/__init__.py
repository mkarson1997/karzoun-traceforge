"""Azure Data Explorer query backend for the production TraceForge viewer."""

from .repository import KustoTraceRepository

__all__ = ["KustoTraceRepository"]

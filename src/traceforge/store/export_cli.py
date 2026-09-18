from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TextIO

from traceforge.store.repository import TraceRepository


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export sanitized TraceForge spans as JSONL")
    parser.add_argument("--db", default="./traceforge.db")
    parser.add_argument("--session")
    parser.add_argument("--task")
    parser.add_argument("--organization")
    parser.add_argument("--limit", type=int, default=10_000)
    parser.add_argument("--output", help="Output file; stdout when omitted")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    repository = TraceRepository(Path(args.db))
    rows = repository.export_spans(
        session_id=args.session,
        task_id=args.task,
        limit=args.limit,
        organization_id=args.organization,
    )

    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="\n") as stream:
            _write_rows(rows, stream)
    else:
        _write_rows(rows, sys.stdout)
    return 0


def _write_rows(rows: list[dict[str, object]], stream: TextIO) -> None:
    for row in rows:
        json.dump(row, stream, separators=(",", ":"), ensure_ascii=False)
        stream.write("\n")


if __name__ == "__main__":
    raise SystemExit(main())

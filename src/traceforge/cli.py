from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any

from traceforge.privacy import PrivacyPolicy, RedactionMode, Scrubber


def _load_json(path: str | None) -> Any:
    if path:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    return json.load(sys.stdin)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Scrub secrets and PII from telemetry JSON")
    parser.add_argument("path", nargs="?", help="JSON file; reads stdin when omitted")
    parser.add_argument(
        "--mode",
        choices=[mode.value for mode in RedactionMode],
        default=RedactionMode.REDACT.value,
    )
    parser.add_argument("--no-pii", action="store_true", help="Do not redact PII patterns")
    parser.add_argument(
        "--show-findings",
        action="store_true",
        help="Write findings to stderr without exposing matched values",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    mode = RedactionMode(args.mode)
    key = os.getenv("TRACEFORGE_TOKENIZATION_KEY")
    scrubber = Scrubber(
        PrivacyPolicy(mode=mode, redact_pii=not args.no_pii),
        tokenization_key=key.encode() if key else None,
    )
    result = scrubber.scrub(_load_json(args.path))
    json.dump(result.value, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    if args.show_findings:
        for finding in result.findings:
            print(f"{finding.kind}\t{finding.path}\t{finding.detail}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

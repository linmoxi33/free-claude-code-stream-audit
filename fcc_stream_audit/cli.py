"""Command line entry point for fcc-stream-audit."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .core import (
    ERROR,
    StreamInputError,
    build_report,
    exit_code,
    minimize_witness,
    parse_stream,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fcc-stream-audit",
        description="Audit a captured OpenAI chat stream for fragmented tool-call names.",
    )
    parser.add_argument("capture", nargs="?", help="SSE/JSONL capture file; omit or - for stdin")
    parser.add_argument("--json", action="store_true", help="emit the machine-readable report")
    parser.add_argument(
        "--strict", action="store_true", help="treat warnings as failures (exit 1)"
    )
    parser.add_argument(
        "--minimize",
        action="store_true",
        help="for each truncated tool name, print a 1-minimal chunk witness",
    )
    parser.add_argument("--version", action="version", version="fcc-stream-audit 1.0.0")
    return parser


def _read(path: str | None) -> tuple[str, str]:
    if path in (None, "-"):
        return sys.stdin.read(), "<stdin>"
    try:
        return Path(path).read_text(encoding="utf-8"), path
    except OSError as exc:
        raise StreamInputError(f"cannot read {path}: {exc.strerror}") from exc


def _text_report(report: dict[str, Any]) -> str:
    lines = [
        f"source: {report['source']}",
        f"chunks: {report['chunks']}  tool_calls: {report['summary']['tool_calls']}  "
        f"errors: {report['summary']['errors']}  warnings: {report['summary']['warnings']}",
    ]
    for call in report["tool_calls"]:
        lines.append(
            f"  tool[{call['index']}] id={call['id']} "
            f"complete={call['complete_name']!r} emit_early={call['emit_early_name']!r} "
            f"fragments={len(call['name_fragments'])}"
        )
    for finding in report["findings"]:
        position = "" if finding["position"] is None else f" @line {finding['position']}"
        lines.append(f"{finding['level'].upper():7} {finding['code']}{position}: {finding['message']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        text, source = _read(args.capture)
        report = build_report(text, source=source)
        witnesses = []
        if args.minimize:
            chunks, _ = parse_stream(text)
            for call in report["tool_calls"]:
                if call["emit_early_name"] != call["complete_name"]:
                    witness, verified = minimize_witness(
                        chunks, call["index"], call["complete_name"], call["emit_early_name"]
                    )
                    witnesses.append(
                        {
                            "tool_index": call["index"],
                            "positions": [c.position for c in witness],
                            "chunks": len(witness),
                            "one_minimal_verified": verified,
                        }
                    )
    except StreamInputError as exc:
        print(f"fcc-stream-audit: {exc}", file=sys.stderr)
        return 2

    if args.json:
        payload = dict(report)
        if args.minimize:
            payload["witnesses"] = witnesses
        print(json.dumps(payload, indent=2, sort_keys=False))
    else:
        print(_text_report(report))
        for witness in witnesses:
            print(
                f"witness tool[{witness['tool_index']}]: lines {witness['positions']} "
                f"({witness['chunks']} chunks, 1-minimal={witness['one_minimal_verified']})"
            )
    return exit_code(report, strict=args.strict)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

"""Reassemble OpenAI-style streaming tool_calls and expose fragmenting risk.

The audit is converter-independent: it consumes a captured OpenAI chat
completion stream (SSE `data:` lines or JSONL) and compares two plausible
readings of ``tool_calls[].function.name``:

* ``complete_name``  - every name fragment concatenated (the correct reading)
* ``emit_early_name`` - the first non-empty fragment only (what a client that
  opens the tool block on the first fragment would show, cf. upstream issue)

No network access, no third-party dependencies.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable

TOOL_NAME = "fcc-stream-audit"
VERSION = "1.0.0"

ERROR = "error"
WARNING = "warning"


class StreamInputError(ValueError):
    """Raised when the input is not a parseable OpenAI stream capture."""


@dataclass
class Chunk:
    position: int          # 1-based line/event position in the source file
    payload: dict[str, Any]


@dataclass
class ToolCallState:
    index: int
    id: str | None = None
    fragments: list[str] = field(default_factory=list)
    argument_fragments: list[str] = field(default_factory=list)
    first_name_position: int | None = None
    first_argument_position: int | None = None
    id_positions: list[int] = field(default_factory=list)


@dataclass
class Finding:
    code: str
    level: str
    tool_index: int | None
    position: int | None
    message: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "level": self.level,
            "tool_index": self.tool_index,
            "position": self.position,
            "message": self.message,
        }


def _iter_payloads(text: str) -> Iterable[tuple[int, dict[str, Any]]]:
    lines = text.splitlines()
    sse = [(i + 1, line) for i, line in enumerate(lines) if line.startswith("data:")]
    if sse:
        for position, line in sse:
            body = line[5:].strip()
            if body == "[DONE]":
                yield position, {"__done__": True}
                continue
            try:
                obj = json.loads(body)
            except json.JSONDecodeError as exc:
                raise StreamInputError(f"line {position}: invalid JSON in data: line ({exc.msg})") from exc
            if not isinstance(obj, dict):
                raise StreamInputError(f"line {position}: data event is not a JSON object")
            yield position, obj
        return
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise StreamInputError(
                f"line {i + 1}: neither an SSE data: line nor a JSON object ({exc.msg})"
            ) from exc
        if not isinstance(obj, dict):
            raise StreamInputError(f"line {i + 1}: JSONL entry is not a JSON object")
        yield i + 1, obj


def parse_stream(text: str) -> tuple[list[Chunk], bool]:
    """Return (chunks, saw_done) from an SSE or JSONL capture."""
    chunks: list[Chunk] = []
    saw_done = False
    for position, payload in _iter_payloads(text):
        if payload.get("__done__"):
            saw_done = True
            continue
        chunks.append(Chunk(position=position, payload=payload))
    if not chunks:
        raise StreamInputError("no OpenAI chat completion chunks found")
    return chunks, saw_done


def _tool_deltas(chunk: Chunk) -> list[dict[str, Any]]:
    choices = chunk.payload.get("choices")
    if not isinstance(choices, list):
        return []
    deltas: list[dict[str, Any]] = []
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        for key in ("delta", "message"):
            container = choice.get(key)
            if not isinstance(container, dict):
                continue
            calls = container.get("tool_calls")
            if isinstance(calls, list):
                deltas.extend(c for c in calls if isinstance(c, dict))
    return deltas


def collect(chunks: list[Chunk]) -> dict[int, ToolCallState]:
    states: dict[int, ToolCallState] = {}
    for chunk in chunks:
        for call in _tool_deltas(chunk):
            index = call.get("index")
            if not isinstance(index, int):
                # Non-streaming message.tool_calls often omit "index"; use order.
                index = len(states) if index is None else index
            state = states.setdefault(index, ToolCallState(index=index))
            call_id = call.get("id")
            if isinstance(call_id, str) and call_id:
                if state.id is None:
                    state.id = call_id
                elif state.id != call_id:
                    state.id_positions.append(chunk.position)
            function = call.get("function")
            if not isinstance(function, dict):
                continue
            name = function.get("name")
            if isinstance(name, str) and name:
                if state.first_name_position is None:
                    state.first_name_position = chunk.position
                state.fragments.append(name)
            arguments = function.get("arguments")
            if isinstance(arguments, str) and arguments:
                if state.first_argument_position is None:
                    state.first_argument_position = chunk.position
                state.argument_fragments.append(arguments)
    return states


def _json_state(raw: str) -> tuple[bool, str | None]:
    try:
        json.loads(raw)
    except json.JSONDecodeError as exc:
        return False, exc.msg
    return True, None


def build_report(text: str, source: str = "<stdin>") -> dict[str, Any]:
    chunks, saw_done = parse_stream(text)
    states = collect(chunks)
    calls: list[dict[str, Any]] = []
    findings: list[Finding] = []

    for index in sorted(states):
        state = states[index]
        complete = "".join(state.fragments)
        emit_early = state.fragments[0] if state.fragments else ""
        replace_name = state.fragments[-1] if state.fragments else ""
        raw_args = "".join(state.argument_fragments)
        args_valid, args_error = _json_state(raw_args) if raw_args else (True, None)

        calls.append(
            {
                "index": index,
                "id": state.id,
                "complete_name": complete,
                "emit_early_name": emit_early,
                "replace_name": replace_name,
                "name_fragments": list(state.fragments),
                "first_name_position": state.first_name_position,
                "arguments": raw_args,
                "arguments_valid": args_valid,
            }
        )

        if len(state.fragments) > 1:
            if emit_early != complete:
                findings.append(
                    Finding(
                        "TOOL_NAME_TRUNCATED_EARLY",
                        ERROR,
                        index,
                        state.first_name_position,
                        f"tool {index} name is assembled from {len(state.fragments)} fragments; "
                        f"opening the block on the first fragment yields {emit_early!r} "
                        f"instead of {complete!r}",
                    )
                )
            else:
                findings.append(
                    Finding(
                        "TOOL_NAME_FRAGMENTED",
                        WARNING,
                        index,
                        state.first_name_position,
                        f"tool {index} name arrives in {len(state.fragments)} fragments",
                    )
                )
        if state.first_argument_position is not None and state.first_name_position is None:
            findings.append(
                Finding(
                    "ARGUMENTS_WITHOUT_NAME",
                    ERROR,
                    index,
                    state.first_argument_position,
                    f"tool {index} streams arguments without ever streaming a name",
                )
            )
        if (
            state.first_name_position is not None
            and state.first_argument_position is not None
            and state.first_argument_position < state.first_name_position
        ):
            findings.append(
                Finding(
                    "ARGUMENTS_BEFORE_NAME",
                    WARNING,
                    index,
                    state.first_argument_position,
                    f"tool {index} streams arguments before the name",
                )
            )
        if not args_valid:
            findings.append(
                Finding(
                    "ARGUMENTS_INVALID_JSON",
                    ERROR,
                    index,
                    state.first_argument_position,
                    f"tool {index} arguments do not parse as JSON: {args_error}",
                )
            )
        for position in state.id_positions:
            findings.append(
                Finding(
                    "TOOL_CALL_ID_CHANGED",
                    ERROR,
                    index,
                    position,
                    f"tool {index} changes its id from {state.id!r} mid-stream",
                )
            )

    if not saw_done:
        findings.append(
            Finding("STREAM_NO_DONE", WARNING, None, None, "capture has no [DONE]/finish marker")
        )
    if not calls:
        findings.append(
            Finding("NO_TOOL_CALLS", WARNING, None, None, "stream contains no tool_calls deltas")
        )

    errors = sum(1 for f in findings if f.level == ERROR)
    warnings = sum(1 for f in findings if f.level == WARNING)
    return {
        "tool": TOOL_NAME,
        "version": VERSION,
        "source": source,
        "chunks": len(chunks),
        "saw_done": saw_done,
        "tool_calls": calls,
        "findings": [f.as_dict() for f in findings],
        "summary": {"tool_calls": len(calls), "errors": errors, "warnings": warnings},
    }


def exit_code(report: dict[str, Any], strict: bool = False) -> int:
    errors = report["summary"]["errors"]
    warnings = report["summary"]["warnings"]
    if errors:
        return 1
    if strict and warnings:
        return 1
    return 0


def _predicate(chunks: list[Chunk], index: int, complete: str, emit_early: str) -> bool:
    state = collect(chunks).get(index)
    if state is None:
        return False
    return "".join(state.fragments) == complete and state.fragments[0] == emit_early


def minimize_witness(
    chunks: list[Chunk], index: int, complete: str, emit_early: str
) -> tuple[list[Chunk], bool]:
    """Greedy 1-minimal slice that still reproduces the name divergence.

    Returns (witness_chunks, verified) where ``verified`` re-runs the removal
    check on the finished witness: dropping any single remaining chunk must
    break the divergence.
    """
    current = list(chunks)
    changed = True
    while changed:
        changed = False
        for chunk in list(current):
            trial = [c for c in current if c is not chunk]
            if _predicate(trial, index, complete, emit_early):
                current = trial
                changed = True
    verified = _predicate(current, index, complete, emit_early) and not any(
        _predicate([c for c in current if c is not chunk], index, complete, emit_early)
        for chunk in current
    )
    return current, verified


def _delta_index(chunk: Chunk) -> int | None:
    deltas = _tool_deltas(chunk)
    if not deltas:
        return None
    index = deltas[0].get("index")
    return index if isinstance(index, int) else None

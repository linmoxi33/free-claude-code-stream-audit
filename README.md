# fcc-stream-audit

Offline audit for OpenAI-compatible **streaming tool calls whose function name arrives in fragments** — it reassembles the capture, shows what an emit-early client would print, and shrinks a failing capture to the smallest set of chunks that still reproduces the truncation.

Why now: providers split `tool_calls[].function.name` across chunks (`"Re"` then `"ad"`). A proxy that opens the tool block on the first fragment sends `name: "Re"` to the harness, and the turn fails with "tool not found" long after the stream looked healthy.

Upstream: https://github.com/Alishahryar1/free-claude-code

## What the upstream does, and what it lacks

Free Claude Code translates OpenAI-compatible chat streams into Anthropic-style `tool_use` blocks. Its own open issue reports the failure mode this tool audits:

- [Alishahryar1/free-claude-code#2342 — "Streamed OpenAI Chat tool calls whose function name arrives in fragments start the tool_use block with a truncated name"](https://github.com/Alishahryar1/free-claude-code/issues/2342) (open, filed 2026-11-10). The reported repro is two chunks: `{"index":0,"id":"call_1","function":{"name":"Re"}}` then `{"index":0,"function":{"name":"ad","arguments":"{\"file_path\":\"a\"}"}}`; the emitted `content_block_start` carries `name: "Re"` instead of `"Read"`. The issue also states the non-streaming collector merges fragments correctly, so only the streaming path is affected.

What upstream does **not** ship is a way to look at an already-captured stream and answer three questions without running the proxy: *which tool call has a fragmented name, what name would an emit-early reader print, and what is the smallest chunk subset that still reproduces the divergence?* That is the gap this repository fills.

## What this tool does / does not do

Does:

- Parses an SSE (`data: {...}` lines, `[DONE]` optional) or JSONL capture of OpenAI chat completion chunks — no network, no SDK.
- Reassembles every `tool_calls[index]` three ways: `complete_name` (all fragments concatenated), `emit_early_name` (first non-empty fragment), `replace_name` (last fragment).
- Flags `TOOL_NAME_TRUNCATED_EARLY` when `emit_early_name != complete_name`, plus invalid assembled arguments JSON, arguments without a name, a changed `id`, and a missing `[DONE]`.
- With `--minimize`, greps out a **1-minimal chunk witness** and re-verifies it: removing any single remaining chunk must destroy the divergence.
- Machine-readable `--json`; exit codes `0` clean, `1` findings, `2` usage/input error.

Does not:

- It does not run, call, or patch the upstream proxy. It never contacts a provider.
- It does not judge whether a given provider is "allowed" to fragment; it only shows what the capture would produce under each reading.

## Install

Python 3.9+, standard library only. No `pip install`.

```bash
git clone <this-repo> && cd free-claude-code-stream-audit
python3 bin/fcc-stream-audit --version
```

## Usage

```bash
python3 bin/fcc-stream-audit examples/in/fragmented-name.sse --minimize
python3 bin/fcc-stream-audit examples/in/fragmented-name.sse --json --minimize
python3 bin/fcc-stream-audit examples/in/clean.sse            # rc=0
cat capture.sse | python3 bin/fcc-stream-audit - --json       # stdin
```

`--json` output (trimmed, real run against `examples/in/fragmented-name.sse`):

```json
{
  "tool": "fcc-stream-audit",
  "source": "examples/in/fragmented-name.sse",
  "chunks": 6,
  "tool_calls": [
    {"index": 0, "id": "call_1", "complete_name": "Read",
     "emit_early_name": "Re", "replace_name": "ad",
     "name_fragments": ["Re", "ad"], "arguments_valid": true}
  ],
  "findings": [
    {"code": "TOOL_NAME_TRUNCATED_EARLY", "level": "error", "tool_index": 0,
     "position": 3, "message": "tool 0 name is assembled from 2 fragments; ..."}
  ],
  "witnesses": [{"tool_index": 0, "positions": [3, 5], "chunks": 2,
                 "one_minimal_verified": true}],
  "summary": {"tool_calls": 2, "errors": 1, "warnings": 0}
}
```

Exit codes:

| code | meaning |
| --- | --- |
| 0 | no error findings |
| 1 | at least one error finding (or any warning with `--strict`) |
| 2 | unreadable file, unparseable capture, or no chunks |

## 本轮创新

First tool that treats a captured tool-call stream as a **falsifiable contract** and emits a *verified 1-minimal chunk witness* for the fragmented-name failure: instead of restating issue #2342, it recomputes both readings over the capture, then proves the witness minimal by re-running the removal check on every remaining chunk (`one_minimal_verified`). The ledger has format linters and static config auditors, but no runtime stream witness like this.

## 与同类项目的差异

Search performed (2026-11-13): `gh search repos "openai streaming tool call"` (no hits) and `gh search repos "sse tool call"` (7 hits, all ≤2 stars, all proxies/adapters). No existing project audits a capture for fragmented tool names or minimizes a reproducing chunk set; proxies fix the problem internally, this tool proves it from the outside.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

20 stdlib tests: 4 parser, 7 reassembly, 2 witness, 5 CLI process-level (exit code 0/1/2, JSON parseability, stdin/file equivalence). All run offline against the files in `examples/in/`.

## Limitations

- The example captures are hand-built from the documented chunk shape in issue #2342's repro, not recorded from a live provider. Treat them as format fixtures, not as evidence about any specific provider.
- Only `choices[].delta`/`choices[].message` `tool_calls` are read; Responses-API `response.*` events are out of scope.
- The "emit early" reading models one specific failure policy (open the block on the first non-empty fragment). A proxy with a different policy needs a different comparison; the complete/naive readings and the witness machinery still apply.
- JSON validity is checked on the fully joined argument string only; per-fragment validity is meaningless by design.

## License

MIT — see `LICENSE`.

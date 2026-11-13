import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fcc_stream_audit.core import (  # noqa: E402
    StreamInputError,
    build_report,
    exit_code,
    minimize_witness,
    parse_stream,
)

CLI = ROOT / "bin" / "fcc-stream-audit"
FRAGMENTED = ROOT / "examples" / "in" / "fragmented-name.sse"
CLEAN = ROOT / "examples" / "in" / "clean.sse"
BROKEN = ROOT / "examples" / "in" / "broken-arguments.jsonl"


def run(*args):
    return subprocess.run(
        [sys.executable, str(CLI), *args], capture_output=True, text=True, cwd=ROOT
    )


class ParseTests(unittest.TestCase):
    def test_parse_sse_skips_done(self):
        chunks, saw_done = parse_stream(FRAGMENTED.read_text())
        self.assertTrue(saw_done)
        self.assertEqual(len(chunks), 6)

    def test_parse_jsonl(self):
        chunks, saw_done = parse_stream(BROKEN.read_text())
        self.assertFalse(saw_done)
        self.assertEqual(len(chunks), 3)

    def test_parse_rejects_garbage(self):
        with self.assertRaises(StreamInputError):
            parse_stream("this is not a stream\n")

    def test_parse_rejects_empty(self):
        with self.assertRaises(StreamInputError):
            parse_stream("\n\n")


class ReassemblyTests(unittest.TestCase):
    def test_emit_early_is_first_fragment(self):
        report = build_report(FRAGMENTED.read_text())
        call = report["tool_calls"][0]
        self.assertEqual(call["complete_name"], "Read")
        self.assertEqual(call["emit_early_name"], "Re")
        self.assertEqual(call["name_fragments"], ["Re", "ad"])

    def test_complete_arguments_are_joined(self):
        report = build_report(FRAGMENTED.read_text())
        self.assertEqual(json.loads(report["tool_calls"][0]["arguments"]), {"file_path": "a"})

    def test_second_call_is_not_flagged_for_fragmentation(self):
        report = build_report(FRAGMENTED.read_text())
        codes = {(f["code"], f["tool_index"]) for f in report["findings"]}
        self.assertNotIn(("TOOL_NAME_FRAGMENTED", 1), codes)
        self.assertNotIn(("TOOL_NAME_TRUNCATED_EARLY", 1), codes)

    def test_clean_stream_exits_zero(self):
        report = build_report(CLEAN.read_text())
        self.assertEqual(report["summary"]["errors"], 0)
        self.assertEqual(exit_code(report), 0)

    def test_truncation_is_an_error(self):
        report = build_report(FRAGMENTED.read_text())
        self.assertEqual(report["summary"]["errors"], 1)
        self.assertEqual(exit_code(report), 1)

    def test_invalid_arguments_detected(self):
        report = build_report(BROKEN.read_text())
        codes = [f["code"] for f in report["findings"]]
        self.assertIn("ARGUMENTS_INVALID_JSON", codes)
        self.assertFalse(report["tool_calls"][0]["arguments_valid"])

    def test_missing_done_is_warning_only(self):
        report = build_report(BROKEN.read_text())
        warnings = [f["code"] for f in report["findings"] if f["level"] == "warning"]
        self.assertIn("STREAM_NO_DONE", warnings)
        self.assertEqual(exit_code(report, strict=False), 1)  # still an error from JSON

    def test_strict_promotes_warnings_to_failure(self):
        text = 'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"name":"Read"}}]}}]}\n'
        report = build_report(text)
        self.assertEqual(exit_code(report, strict=False), 0)
        self.assertEqual(exit_code(report, strict=True), 1)

    def test_arguments_without_name_is_error(self):
        text = (
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"x",'
            '"function":{"arguments":"{}"}}]}}]}\n'
        )
        codes = [f["code"] for f in build_report(text)["findings"]]
        self.assertIn("ARGUMENTS_WITHOUT_NAME", codes)


class WitnessTests(unittest.TestCase):
    def test_witness_is_minimal_and_verified(self):
        chunks, _ = parse_stream(FRAGMENTED.read_text())
        witness, verified = minimize_witness(chunks, 0, "Read", "Re")
        self.assertTrue(verified)
        self.assertEqual([c.position for c in witness], [3, 5])

    def test_witness_drops_unrelated_chunks(self):
        chunks, _ = parse_stream(FRAGMENTED.read_text())
        witness, _ = minimize_witness(chunks, 0, "Read", "Re")
        positions = {c.position for c in witness}
        self.assertNotIn(7, positions)  # second tool call chunk
        self.assertNotIn(9, positions)  # finish chunk


class CliTests(unittest.TestCase):
    def test_json_output_is_parseable(self):
        proc = run(str(FRAGMENTED), "--json", "--minimize")
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["tool"], "fcc-stream-audit")
        self.assertEqual(proc.returncode, 1)
        self.assertTrue(payload["witnesses"][0]["one_minimal_verified"])

    def test_clean_exit_code(self):
        proc = run(str(CLEAN))
        self.assertEqual(proc.returncode, 0)

    def test_unreadable_file_exits_2(self):
        proc = run("/nonexistent/capture.sse")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("cannot read", proc.stderr)

    def test_garbage_input_exits_2(self):
        proc = subprocess.run(
            [sys.executable, str(CLI)],
            input="not a stream",
            capture_output=True,
            text=True,
            cwd=ROOT,
        )
        self.assertEqual(proc.returncode, 2)

    def test_stdin_matches_file_output(self):
        file_proc = run(str(FRAGMENTED), "--json")
        stdin_proc = subprocess.run(
            [sys.executable, str(CLI), "-", "--json"],
            input=FRAGMENTED.read_text(),
            capture_output=True,
            text=True,
            cwd=ROOT,
        )
        a = json.loads(file_proc.stdout)
        b = json.loads(stdin_proc.stdout)
        a.pop("source")
        b.pop("source")
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()

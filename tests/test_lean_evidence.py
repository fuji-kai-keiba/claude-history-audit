import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from claude_history_audit.evidence import TEXT_LIMIT, lean, read_selected


class LeanEvidenceTests(unittest.TestCase):
    def test_opaque_payloads_are_dropped_before_truncation(self):
        content = [
            {"type": "thinking", "thinking": "", "signature": "S" * 9000},
            {"type": "redacted_thinking", "data": "R" * 5000},
            {"type": "text", "text": "visible answer"},
            {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "/synthetic/a.txt"}},
        ]
        user = [{"type": "tool_result", "tool_use_id": "t1", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "B" * 20000}},
            {"type": "text", "text": "result text"}]}]
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "s.jsonl"
            rows = [{"type": "assistant", "timestamp": "2026-10-01T00:00:00Z", "message": {"role": "assistant", "content": content}},
                    {"type": "user", "timestamp": "2026-10-01T00:00:01Z", "message": {"role": "user", "content": user}}]
            raw = [json.dumps(r) + "\n" for r in rows]
            path.write_bytes("".join(raw).encode("utf-8"))
            records = read_selected({"f": str(path)}, [{"file": "f", "line": 1}])
        first, second = records[("f", 1)], records[("f", 2)]
        for record in (first, second):
            self.assertFalse(record["truncated"])
            self.assertLess(len(record["untrusted_content"]), TEXT_LIMIT)
        self.assertIn("visible answer", first["untrusted_content"])
        self.assertIn("/synthetic/a.txt", first["untrusted_content"])
        self.assertNotIn("SSSS", first["untrusted_content"])
        self.assertNotIn("RRRR", first["untrusted_content"])
        self.assertIn('"omitted_chars": 5000', first["untrusted_content"])
        self.assertIn("result text", second["untrusted_content"])
        self.assertNotIn("BBBB", second["untrusted_content"])
        self.assertIn('"omitted_chars": 20000', second["untrusted_content"])
        # The integrity hash still covers the original line.
        self.assertEqual(first["sha256"], hashlib.sha256(raw[0].encode("utf-8")).hexdigest())

    def test_lean_keeps_ordinary_values(self):
        value = {"type": "text", "text": "a", "nested": [{"type": "text", "text": "b"}], "source": "plain"}
        self.assertEqual(lean(value), value)
        self.assertEqual(lean({"type": "thinking", "thinking": "summary", "signature": "x"}), {"type": "thinking", "thinking": "summary"})


if __name__ == "__main__":
    unittest.main()

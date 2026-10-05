import contextlib
import copy
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from claude_history_audit.audit import audit, discover, load_prices, parse_time, read_records
from claude_history_audit.cli import main
from claude_history_audit.report import render_html, write_reports

NOW = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)


def response(mid="msg-1", session="session-1", timestamp="2026-10-04T10:00:00Z", usage=None, content=None):
    return {"type": "assistant", "sessionId": session, "timestamp": timestamp, "requestId": "req-" + mid,
        "message": {"id": mid, "role": "assistant", "model": "claude-sonnet-4-6", "usage": usage if usage is not None else {
            "input_tokens": 100, "output_tokens": 200, "cache_creation_input_tokens": 1000, "cache_read_input_tokens": 9000,
            "cache_creation": {"ephemeral_5m_input_tokens": 1000, "ephemeral_1h_input_tokens": 0}},
            "content": content if content is not None else [{"type": "text", "text": "synthetic answer"}]}}


def tool(tid, name="Read", path="/synthetic/input.pdf", **kwargs):
    return {"type": "tool_use", "id": tid, "name": name, "input": dict(file_path=path, **kwargs)}


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "input"
        self.source.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def write(self, rows, name="one.jsonl"):
        path = self.source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        return path

    def run_audit(self, **kwargs):
        return audit([self.source], now=NOW, **kwargs)[0]

    def test_streamed_blocks_and_copied_transcripts_not_double_counted(self):
        first = response(content=[tool("t1")])
        final = copy.deepcopy(first)
        final["message"]["usage"]["output_tokens"] = 300
        self.write([first, final])
        self.write([first, final], "copied.jsonl")
        r = self.run_audit()
        self.assertEqual(r["coverage"]["unique_requests"], 1)
        self.assertEqual(r["coverage"]["duplicate_usage_records"], 3)
        self.assertEqual(r["totals"]["output_tokens"], 300)
        self.assertEqual(r["totals"]["input_tokens"], 100)
        self.assertEqual(r["metrics"]["tool_calls"], 1)

    def test_nested_thinking_and_iterations_not_added_twice(self):
        row = response()
        u = row["message"]["usage"]
        u["output_tokens_details"] = {"thinking_tokens": 50}
        u["iterations"] = [{"input_tokens": 100, "output_tokens": 200}]
        self.write([row])
        self.assertEqual(self.run_audit()["totals"]["output_tokens"], 200)

    def test_request_id_fallback_and_missing_identifiers(self):
        row = response()
        del row["message"]["id"]
        missing = response("msg2")
        del missing["message"]["id"]
        del missing["requestId"]
        self.write([row, row, missing])
        r = self.run_audit()
        self.assertEqual(r["coverage"]["unique_requests"], 2)
        self.assertEqual(r["coverage"]["requests_without_stable_id"], 1)

    def test_timezone_and_exclusive_window(self):
        self.write([response("a", timestamp="2026-10-04T00:00:00+09:00"),
                    response("b", timestamp="2026-10-04T00:00:00Z"),
                    response("c", timestamp="2026-10-05T00:00:00Z")])
        r = self.run_audit(since=parse_time("2026-10-04T00:00:00Z"), until=parse_time("2026-10-05T00:00:00Z"))
        self.assertEqual(r["coverage"]["unique_requests"], 1)
        self.assertEqual(r["coverage"]["outside_window"], 2)

    def test_malformed_missing_time_and_incomplete_usage_are_visible(self):
        row = response()
        row.pop("timestamp")
        path = self.write([row, response("partial", usage={"input_tokens": 50, "output_tokens": -1})])
        with path.open("a") as f:
            f.write('{bad\n[]\n')
        r = self.run_audit()
        self.assertEqual(r["coverage"]["malformed_lines"], 2)
        self.assertEqual(r["coverage"]["missing_timestamps"], 1)
        self.assertEqual(r["coverage"]["complete_usage_requests"], 0)
        self.assertEqual(r["totals"]["output_tokens"], 0)

    def test_invalid_token_types_and_nested_data_do_not_crash(self):
        self.write([response(usage={"input_tokens": True, "output_tokens": "12", "cache_creation_input_tokens": {}, "cache_read_input_tokens": None})])
        self.assertEqual(sum(self.run_audit()["totals"].values()), 0)

    def test_subagent_and_overlapping_roots(self):
        p = self.write([response()])
        self.write([response("child", session="child")], "one/subagents/agent-a.jsonl")
        r, _ = audit([self.source, p], now=NOW)
        self.assertEqual(r["coverage"]["files_found"], 2)
        self.assertEqual(r["coverage"]["unique_requests"], 2)
        self.assertEqual(sum(s["subagent"] for s in r["sessions"]), 1)

    def test_subagent_using_parent_session_id_is_separate_execution(self):
        self.write([response()])
        self.write([response("child", session="session-1")], "one/subagents/agent-a.jsonl")
        r = self.run_audit()
        self.assertEqual(r["coverage"]["sessions"], 2)
        self.assertEqual(sum(s["subagent"] for s in r["sessions"]), 1)

    def test_compaction_copies_are_deduplicated(self):
        marker = {"type": "system", "subtype": "compact_boundary", "uuid": "compact-1", "sessionId": "session-1", "timestamp": "2026-10-04T10:00:00Z"}
        self.write([response(), marker])
        self.write([marker], "copy.jsonl")
        self.assertEqual(sum(s["compactions"] for s in self.run_audit()["sessions"]), 1)

    def test_repeated_read_range_and_edits(self):
        self.write([response("a", content=[tool("a", offset=1, limit=10)]),
                    response("b", content=[tool("b", offset=1, limit=10)]),
                    response("c", content=[tool("c", offset=11, limit=10)]),
                    response("d", content=[tool("d", name="Edit")]),
                    response("e", content=[tool("e", offset=1, limit=10)])])
        r = self.run_audit()
        self.assertEqual(r["metrics"]["repeated_reads"], 1)
        self.assertEqual(r["metrics"]["pdf_read_calls"], 4)

    def test_tool_errors_are_deduplicated(self):
        err = {"type": "user", "sessionId": "session-1", "timestamp": "2026-10-04T10:00:01Z",
               "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "is_error": True, "content": "failed"}]}}
        self.write([response(content=[tool("t1")]), err, err])
        self.assertEqual(self.run_audit()["metrics"]["tool_errors"], 1)

    def test_only_text_user_prompts_drive_workload_hints(self):
        user = {"type": "user", "uuid": "u1", "sessionId": "session-1", "timestamp": "2026-10-04T10:00:00Z",
                "message": {"content": "月次報告を作成してください"}}
        self.write([response(), user, user])
        session = self.run_audit()["sessions"][0]
        self.assertEqual(session["prompts"], 1)
        self.assertEqual(session["workload_hint"], "社内会議")

    def test_private_content_is_not_in_aggregate_outputs(self):
        secret = "PrivateCustomer_EMAIL@example.test<script>alert(1)</script>"
        row = response(session=secret, content=[{"type": "text", "text": secret}, tool("t", path="/Users/SecretClient/contract.pdf"),
            {"type": "tool_use", "id": "mcp", "name": "mcp__SecretClient__private", "input": {}}])
        row["message"]["model"] = "claude-opus-SecretClient"
        self.write([row])
        r, mapping = audit([self.source], now=NOW)
        dest = write_reports(self.root / "out", r, mapping)
        for name in ("report.json", "summary.md", "report.html"):
            data = (dest / name).read_text()
            self.assertNotIn(secret, data)
            self.assertNotIn("SecretClient", data)
            self.assertNotIn(str(self.source), data)
        self.assertIn(secret, (dest / "local-map.json").read_text())
        self.assertEqual((dest / "local-map.json").stat().st_mode & 0o777, 0o600)

    def test_html_escapes_all_dynamic_values(self):
        self.write([response()])
        r = self.run_audit()
        r["findings"][0]["title"] = '<img src=x onerror="alert(1)">'
        rendered = render_html(r)
        self.assertNotIn('<img src=x', rendered)
        self.assertIn('&lt;img src=x', rendered)

    def test_prices_known_ttl_and_unknown_ttl_range(self):
        self.write([response()])
        prices = {"models": {"claude-sonnet-4-6": {"input": 3, "output": 15, "cache_read": .3, "cache_write_5m": 3.75, "cache_write_1h": 6}}}
        r = self.run_audit(prices=prices)
        self.assertAlmostEqual(r["cost"]["usd_range"][0], .00975)
        self.assertEqual(r["cost"]["usd_range"][0], r["cost"]["usd_range"][1])
        row = response()
        row["message"]["usage"].pop("cache_creation")
        self.write([row])
        r = self.run_audit(prices=prices)
        self.assertAlmostEqual(r["cost"]["usd_range"][0], .00975)
        self.assertAlmostEqual(r["cost"]["usd_range"][1], .012)

    def test_unknown_models_fast_modes_not_priced_as_zero(self):
        prices = {"models": {"claude-sonnet-4-6": {"input": 3, "output": 15, "cache_read": .3, "cache_write_5m": 3.75, "cache_write_1h": 6}}}
        row = response()
        row["message"]["usage"]["speed"] = "fast"
        self.write([row])
        r = self.run_audit(prices=prices)
        self.assertIsNone(r["cost"]["usd_range"])
        self.assertEqual(r["cost"]["unpriced_requests"], 1)

    def test_price_book_rejects_nonfinite_and_negative(self):
        p = self.root / "prices.json"
        for value in (-1, float("nan"), True):
            p.write_text(json.dumps({"models": {"claude-sonnet-4-6": {"input": value}}}))
            with self.assertRaises(ValueError):
                load_prices(p)

    def test_no_source_files_or_no_records_fail(self):
        with self.assertRaises(ValueError):
            self.run_audit()
        self.write([{"type": "metadata"}])
        with self.assertRaises(ValueError):
            self.run_audit()

    def test_symlinks_are_not_followed(self):
        self.write([response()])
        (self.source / "linked.jsonl").symlink_to(self.source / "one.jsonl")
        (self.source / "cycle").symlink_to(self.source, target_is_directory=True)
        files, skipped, _ = discover([self.source])
        self.assertEqual(len(files), 1)
        self.assertEqual(skipped, 2)

    def test_oversized_record_does_not_hide_next_line(self):
        path = self.source / "one.jsonl"
        path.write_bytes(b'x' * 1024 + b'\n' + json.dumps({"type": "ok"}).encode() + b'\n')
        from collections import Counter
        stats = Counter()
        with patch("claude_history_audit.audit.MAX_LINE_BYTES", 100):
            rows = list(read_records(path, stats))
        self.assertEqual(rows, [(2, {"type": "ok"})])
        self.assertEqual(stats["oversized_lines"], 1)

    def test_cli_read_only_no_overwrite_and_explicit_dates(self):
        path = self.write([response()])
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        args = ["--source", str(self.source), "--since", "2026-10-04", "--until", "2026-10-04", "--output", str(self.root / "out")]
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(args), 0)
            self.assertEqual(main(args), 2)
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)
        r = json.loads((self.root / "out" / "report.json").read_text())
        self.assertEqual(r["coverage"]["unique_requests"], 1)

    def test_cli_rejects_output_inside_source(self):
        self.write([response()])
        with contextlib.redirect_stderr(io.StringIO()):
            code = main(["--source", str(self.source), "--all", "--output", str(self.source / "out")])
        self.assertEqual(code, 2)
        self.assertFalse((self.source / "out").exists())

    def test_custom_config_directory(self):
        config = self.root / "config"
        (config / "projects").mkdir(parents=True)
        (config / "projects" / "one.jsonl").write_text(json.dumps(response()) + "\n")
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(config)}), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--all", "--output", str(self.root / "out")]), 0)


if __name__ == "__main__":
    unittest.main()

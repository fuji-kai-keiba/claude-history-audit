import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from claude_history_audit.audit import audit, load_prices
from claude_history_audit.config_audit import audit_config
from claude_history_audit.report import markdown, render_html

NOW = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)
PRICES = load_prices(Path(__file__).resolve().parents[1] / "app" / "claude_history_audit" / "reference_prices.json")
START = datetime(2026, 10, 1, tzinfo=timezone.utc)


def stamp(t):
    return t.isoformat().replace("+00:00", "Z")


def session_rows(sid, n, cwd="/synthetic/project", gap_at=None, model="claude-opus-5-5", seed=0):
    """Context grows by exactly 0.9*output + 0.4*tool_bytes + 300 per request."""
    rows, ctx, t = [], 20000, START + timedelta(hours=seed)
    rows.append({"type": "user", "sessionId": sid, "cwd": cwd, "timestamp": stamp(t), "uuid": sid + "-u0",
                 "message": {"role": "user", "content": "synthetic request"}})
    for i in range(n):
        t += timedelta(seconds=3700 if gap_at == i else 600 if gap_at == -i else 20)
        out = 200 + (i * 37 + seed * 11) % 900
        tool_bytes = 1000 + (i * 53 + seed * 7) % 5000
        rows.append({"type": "assistant", "sessionId": sid, "cwd": cwd, "timestamp": stamp(t), "requestId": f"req-{sid}-{i}",
                     "message": {"id": f"msg-{sid}-{i}", "role": "assistant", "model": model,
                                 "usage": {"input_tokens": 1, "output_tokens": out, "cache_creation_input_tokens": 500,
                                           "cache_read_input_tokens": ctx - 501,
                                           "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 500}},
                                 "content": [{"type": "tool_use", "id": f"tool-{sid}-{i}", "name": "Read", "input": {"file_path": "/synthetic/a.txt"}}]}})
        t += timedelta(seconds=1)
        rows.append({"type": "user", "sessionId": sid, "cwd": cwd, "timestamp": stamp(t), "uuid": f"{sid}-r{i}",
                     "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"tool-{sid}-{i}", "content": "x" * tool_bytes}]}})
        ctx += round(0.9 * out + 0.4 * tool_bytes + 300)
    return rows


class AnatomyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "projects" / "p"
        self.source.mkdir(parents=True)

    def tearDown(self):
        self.temp.cleanup()

    def write(self, name, rows):
        (self.source / name).write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

    def run_audit(self, **kw):
        return audit([self.root / "projects"], since=START - timedelta(days=1), now=NOW, prices=PRICES, deep=True, **kw)

    def test_recovers_growth_model_and_reconciles(self):
        for k in range(3):
            self.write(f"s{k}.jsonl", session_rows(f"s{k}", 120, seed=k))
        report, _ = self.run_audit()
        a = report["anatomy"]
        coef = a["fit"]["coefficients"]
        self.assertAlmostEqual(coef["previous_output_tokens"], 0.9, delta=0.02)
        self.assertAlmostEqual(coef["tool_result_bytes"], 0.4, delta=0.02)
        self.assertGreater(a["fit"]["r2"], 0.99)
        self.assertTrue(a["reconciled"])
        origins = {o["origin"]: o for o in a["origins"]}
        self.assertIn("own_output", origins)
        self.assertIn("tool:Read", origins)
        self.assertIn("startup", origins)
        self.assertAlmostEqual(sum(o["read_tokens"] for o in a["origins"]), a["read_tokens_covered"], delta=len(a["origins"]))
        self.assertGreater(a["structure"]["reuse_multiplier"], 1)
        self.assertEqual(a["structure"]["single_tool_requests"], 360)
        self.assertAlmostEqual(a["structure"]["requests_per_prompt"], 120, delta=1)

    def test_small_samples_do_not_fit(self):
        self.write("s.jsonl", session_rows("s", 30))
        a = self.run_audit()[0]["anatomy"]
        self.assertEqual(a["status"], "structure_only")
        self.assertIsNone(a["fit"]["coefficients"])
        self.assertTrue(a["reconciled"])

    def test_ttl_counterfactual_counts_mid_gaps(self):
        rows = session_rows("s", 250)
        gap = session_rows("g", 40, gap_at=-5, seed=3)
        self.write("s.jsonl", rows)
        self.write("g.jsonl", gap)
        ttl = self.run_audit()[0]["anatomy"]["cache_ttl"]
        self.assertEqual(ttl["status"], "estimated")
        self.assertGreater(ttl["if_5m_added_rewrite_usd"], 0)
        self.assertGreater(ttl["if_5m_write_savings_usd"], 0)

    def test_config_is_allowlisted_and_private(self):
        config = self.root / "cfg"
        project = self.root / "work" / "proj"
        project.mkdir(parents=True)
        config.mkdir()
        (config / "settings.json").write_text(json.dumps({
            "env": {"CLAUDE_CODE_SUBAGENT_MODEL": "opus", "ANTHROPIC_API_KEY": "sk-secret-value", "OTHER": "x"},
            "effortLevel": "xhigh", "autoCompactWindow": 500000, "model": "a very long model name with spaces",
            "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "secret-command --token abc"}]}]},
            "modelSettings": {"claude-opus-5-5": {"effortLevel": "medium"}, "not a model": {"effortLevel": "x"}}}), encoding="utf-8")
        (config / "CLAUDE.md").write_text("rules\n- read this（@" + str(self.root / "entry.md") + "）\n```\n@ignored.md\n```\n", encoding="utf-8")
        (self.root / "entry.md").write_text("e" * 1000, encoding="utf-8")
        (project / "CLAUDE.md").write_text("p" * 2000, encoding="utf-8")
        memory = config / "projects" / __import__("re").sub(r"[^A-Za-z0-9]", "-", str(project)) / "memory"
        memory.mkdir(parents=True)
        (memory / "MEMORY.md").write_text("m" * 3000, encoding="utf-8")
        summary, private = audit_config(config, {"project-1": str(project)}, {"project-1": 4})
        text = json.dumps(summary, ensure_ascii=False)
        for secret in ("sk-secret-value", "secret-command", "ANTHROPIC_API_KEY", str(self.root), "OTHER"):
            self.assertNotIn(secret, text)
        self.assertEqual(summary["env"], {"CLAUDE_CODE_SUBAGENT_MODEL": "opus"})
        self.assertEqual(summary["settings"]["model"], "set")
        self.assertEqual(summary["hook_commands_by_event"], {"Stop": 1})
        self.assertEqual(summary["model_effort"], {"claude-opus-5-5": "medium"})
        kinds = summary["projects"][0]["by_kind"]
        self.assertEqual(kinds["import"], 1000)
        self.assertEqual(kinds["project_claude_md"], 2000)
        self.assertEqual(kinds["memory_index"], 3000)
        self.assertEqual(summary["projects"][0]["sessions"], 4)
        self.assertTrue(any(str(project) in p for p in private["project-1"]))

    def test_config_links_mechanisms_and_reports_render(self):
        for k in range(3):
            self.write(f"s{k}.jsonl", session_rows(f"s{k}", 120, seed=k))
        config = self.root / "cfg"
        config.mkdir()
        (config / "settings.json").write_text(json.dumps({"env": {"CLAUDE_CODE_SUBAGENT_MODEL": "opus"}, "effortLevel": "xhigh"}), encoding="utf-8")
        report, local_map = self.run_audit(config_dir=config)
        mech = {m["code"]: m for m in report["anatomy"]["mechanisms"]}
        self.assertEqual(mech["resend"]["status"], "structural")
        self.assertEqual(mech["own_output"]["linked_settings"]["effortLevel"], "xhigh")
        self.assertIn("projects", local_map)
        dumped = json.dumps(report, ensure_ascii=False)
        self.assertNotIn("/synthetic/project", dumped)
        self.assertNotIn(str(config), dumped)
        self.assertIn("なぜトークンがかかるか", markdown(report))
        self.assertIn("なぜトークンがかかるか", render_html(report))

    def test_forked_copy_is_counted_once(self):
        for k in range(3):
            self.write(f"s{k}.jsonl", session_rows(f"s{k}", 120, seed=k))
        base = self.run_audit()[0]["anatomy"]
        copy = [dict(r, sessionId="fork") for r in session_rows("s0", 120, seed=0)]
        self.write("fork.jsonl", copy)
        a = self.run_audit()[0]["anatomy"]
        self.assertEqual(a["read_tokens_covered"], base["read_tokens_covered"])
        self.assertLessEqual(a["read_tokens_covered"], a["read_tokens_all"])
        self.assertTrue(a["reconciled"])
        self.assertEqual(a["structure"]["requests"], base["structure"]["requests"])

    def test_zero_usage_response_does_not_reset_context(self):
        for k in range(3):
            self.write(f"s{k}.jsonl", session_rows(f"s{k}", 120, seed=k))
        base = {o["origin"]: o["read_tokens"] for o in self.run_audit()[0]["anatomy"]["origins"]}
        rows = session_rows("s0", 120, seed=0)
        t = rows[100]["timestamp"]
        rows.insert(101, {"type": "assistant", "sessionId": "s0", "timestamp": t, "requestId": "req-syn",
                          "message": {"id": "msg-syn", "role": "assistant", "model": "<synthetic>",
                                      "usage": {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0,
                                                "cache_read_input_tokens": 0}, "content": [{"type": "text", "text": "API Error"}]}})
        self.write("s0.jsonl", rows)
        a = self.run_audit()[0]["anatomy"]
        got = {o["origin"]: o["read_tokens"] for o in a["origins"]}
        self.assertAlmostEqual(got["startup"], base["startup"], delta=base["startup"] * 0.01)
        self.assertEqual(a["structure"]["excluded_zero_or_unknown"], 1)

    def test_session_started_before_window_is_carried_in(self):
        rows = session_rows("s", 250)
        self.write("s.jsonl", rows)
        cut = datetime.fromisoformat(rows[200]["timestamp"].replace("Z", "+00:00"))
        report, _ = audit([self.root / "projects"], since=cut, now=NOW, prices=PRICES, deep=True)
        a = report["anatomy"]
        origins = {o["origin"] for o in a["origins"]}
        self.assertIn("carried_in", origins)
        self.assertNotIn("startup", origins)
        self.assertEqual(a["structure"]["true_starts"], 0)

    def test_network_and_outside_imports_are_not_touched(self):
        config = self.root / "cfg"
        config.mkdir()
        outside = Path(tempfile.mkdtemp())
        try:
            (outside / "x.md").write_text("o" * 500, encoding="utf-8")
            unc = "\\\\evil.example\\s\\y.md"
            (config / "CLAUDE.md").write_text("@//evil.example/share/x.md\n@" + unc + "\n@" + str(outside / "x.md") + "\n",
                                              encoding="utf-8")
            from unittest.mock import patch
            import claude_history_audit.config_audit as ca
            with patch.object(ca, "file_size", wraps=ca.file_size) as spy, \
                 patch.object(ca.Path, "home", return_value=self.root / "home"):
                summary, _ = audit_config(config, {"p": str(self.root / "work")}, {})
            touched = [str(c.args[0]) for c in spy.call_args_list]
            self.assertFalse(any("evil.example" in t for t in touched))
            self.assertFalse(any(str(outside) in t for t in touched))
            self.assertGreaterEqual(summary["projects"][0]["unresolved_imports"], 3)
            self.assertNotIn("import", summary["projects"][0]["by_kind"])
        finally:
            import shutil
            shutil.rmtree(outside, ignore_errors=True)

    def test_summary_only_skips_anatomy(self):
        self.write("s.jsonl", session_rows("s", 30))
        report, _ = audit([self.root / "projects"], since=START - timedelta(days=1), now=NOW, prices=PRICES, deep=False)
        self.assertNotIn("anatomy", report)


if __name__ == "__main__":
    unittest.main()

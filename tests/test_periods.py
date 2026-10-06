import contextlib
import io
import json
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from claude_history_audit.audit import audit, load_prices
from claude_history_audit.cli import main
from claude_history_audit.periods import Zone, period_rows, render_table
from claude_history_audit.report import markdown, render_html

NOW = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)
PRICES = Path(__file__).resolve().parents[1] / "docs" / "prices.example.json"


def response(mid, timestamp, model="claude-opus-5-5", output=10):
    return {"type": "assistant", "sessionId": "session-1", "timestamp": timestamp, "requestId": "req-" + mid,
        "message": {"id": mid, "role": "assistant", "model": model, "usage": {
            "input_tokens": 1, "output_tokens": output, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 1000,
            "cache_creation": {"ephemeral_5m_input_tokens": 100, "ephemeral_1h_input_tokens": 0}},
            "content": [{"type": "text", "text": "synthetic"}]}}


class PeriodTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.source = Path(self.temp.name) / "input"
        self.source.mkdir()
        rows = [
            response("a", "2026-09-30T14:59:00Z"),   # JST 2026-09-30 23:59
            response("b", "2026-09-30T15:00:00Z"),   # JST 2026-10-01 00:00
            response("c", "2026-10-04T23:00:00Z", model="claude-fable-5-1"),  # JST 2026-10-05 (Mon)
        ]
        (self.source / "one.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def run_audit(self, zone, prices=None):
        return audit([self.source], now=NOW, zone=zone, prices=prices)[0]

    def test_zone_parsing(self):
        self.assertEqual(Zone("UTC").label, "UTC")
        self.assertEqual(Zone("+0900").label, "+09:00")
        self.assertEqual(Zone("-05:30").tz.utcoffset(None), -timedelta(hours=5, minutes=30))
        self.assertTrue(Zone("local").label.startswith("local ("))
        for bad in ("+15:00", "Not/AZone"):
            with self.assertRaises(ValueError):
                Zone(bad)

    def test_day_boundary_follows_zone(self):
        utc = self.run_audit(Zone("UTC"))["periods"]
        jst = self.run_audit(Zone("+09:00"))["periods"]
        self.assertEqual([r["period"] for r in utc["daily"]], ["2026-09-30", "2026-10-04"])
        self.assertEqual([r["requests"] for r in utc["daily"]], [2, 1])
        self.assertEqual([r["period"] for r in jst["daily"]], ["2026-09-30", "2026-10-01", "2026-10-05"])
        self.assertEqual([r["period"] for r in jst["monthly"]], ["2026-09", "2026-10"])
        self.assertEqual([r["period"] for r in jst["weekly"]], ["2026-09-28", "2026-10-05"])
        self.assertEqual(jst["timezone"], "+09:00")

    def test_period_totals_match_report_totals(self):
        report = self.run_audit(Zone("+09:00"))
        for unit in ("daily", "weekly", "monthly"):
            for field, total in report["totals"].items():
                self.assertEqual(sum(r["tokens"][field] for r in report["periods"][unit]), total)

    def test_prices_flow_into_periods_and_models(self):
        report = self.run_audit(Zone("UTC"), load_prices(PRICES))
        last = report["periods"]["daily"][-1]
        self.assertEqual(last["models"][0]["model"], "claude-fable-5-1")
        # fable-5-1: 1*10 + 10*50 + 1000*0.25 + 100*12.5 per million
        self.assertAlmostEqual(last["cost_usd_range"][0], (10 + 500 + 250 + 1250) / 1e6)
        self.assertEqual(last["priced_requests"], 1)

    def test_unpriced_rows_are_not_shown_as_zero(self):
        rows = period_rows([{"timestamp": "2026-10-01T00:00:00Z", "model": "unknown", "cost_usd_range": None,
                             "usage": {"input_tokens": 1, "output_tokens": 1, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}],
                           Zone("UTC"), "daily")
        self.assertIsNone(rows[0]["cost_usd_range"])
        self.assertIn("未換算", render_table(rows, "daily", "UTC"))

    def test_table_and_reports_render(self):
        report = self.run_audit(Zone("+09:00"), load_prices(PRICES))
        table = render_table(report["periods"]["daily"], "daily", "+09:00", breakdown=True)
        self.assertIn("2026-10-05", table)
        self.assertIn("  claude-fable-5-1", table)
        self.assertIn("合計", table)
        self.assertIn("## 期間別の利用", markdown(report))
        self.assertIn("期間別の利用", render_html(report))

    def test_usage_cli_json_and_dates_in_zone(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["usage", "daily", "--source", str(self.source), "--timezone", "+09:00",
                         "--since", "2026-10-01", "--until", "2026-10-05", "--json"])
        self.assertEqual(code, 0)
        data = json.loads(out.getvalue())
        self.assertEqual([r["period"] for r in data["rows"]], ["2026-10-01", "2026-10-05"])
        self.assertEqual(data["window"]["since"], "2026-09-30T15:00:00Z")
        self.assertFalse(any(Path(self.temp.name).glob("**/report.json")))

    def test_usage_cli_table_and_errors(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["usage", "monthly", "--source", str(self.source), "--timezone", "UTC", "--all"])
        self.assertEqual(code, 0)
        self.assertIn("2026-09", out.getvalue())
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(main(["usage", "--source", str(self.source), "--timezone", "+20:00", "--all"]), 2)
            self.assertEqual(main(["usage", "--source", str(self.source), "--since", "2026-13-01"]), 2)

    def test_usage_cli_without_prices(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["usage", "monthly", "--source", str(self.source), "--timezone", "UTC", "--all", "--no-prices"])
        self.assertEqual(code, 0)
        self.assertIn("未換算", out.getvalue())
        self.assertIn("--no-prices", out.getvalue())


if __name__ == "__main__":
    unittest.main()

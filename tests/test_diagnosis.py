import contextlib
import io
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
from claude_history_audit.audit import audit, load_prices, parse_time
from claude_history_audit.cli import main
from claude_history_audit.report import markdown, render_html
from test_audit import response

ROOT = Path(__file__).resolve().parents[1]
START = datetime(2026, 10, 4, 10, tzinfo=timezone.utc)


def row(mid, seconds, model='claude-sonnet-4-6', usage=None):
    item = response(mid, timestamp=(START + timedelta(seconds=seconds)).isoformat(), usage=usage)
    item['message']['model'] = model
    return item


class DiagnosisTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root/'input'
        self.source.mkdir()
        self.prices = load_prices(ROOT/'app/claude_history_audit/reference_prices.json')

    def tearDown(self):
        self.temp.cleanup()

    def write(self, rows, name='one.jsonl'):
        path = self.source/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(''.join(json.dumps(r)+'\n' for r in rows), encoding='utf-8')

    def report(self, **kwargs):
        return audit([self.source], deep=True, prices=self.prices, **kwargs)[0]

    def test_all_write_buckets_reconcile_without_double_counting(self):
        compact = {'type':'system','sessionId':'session-1','timestamp':(START+timedelta(seconds=3)).isoformat(),
                   'subtype':'compact_boundary','uuid':'compact'}
        self.write([row('a',0),row('b',1),row('c',2,'claude-opus-5'),compact,
                    row('d',4,'claude-opus-5'),row('e',360,'claude-opus-5'),row('f',3961,'claude-opus-5'),
                    row('g',10800),row('h',10801,usage={'cache_creation_input_tokens':1000})])
        r = self.report()
        d = r['deep']['diagnosis']
        buckets = d['cache_writes']['buckets']
        self.assertTrue(d['cache_writes']['reconciles_to_total'])
        self.assertEqual(sum(b['write_tokens'] for b in buckets),r['totals']['cache_creation_input_tokens'])
        self.assertEqual(sum(b['requests'] for b in buckets),8)
        self.assertTrue(all(b['requests']==1 for b in buckets))
        self.assertEqual(sum(b['unpriced_requests'] for b in buckets),1)
        for i in (0,1):
            self.assertAlmostEqual(sum(b['cost_usd_range'][i] for b in buckets if b['cost_usd_range']),
                                   r['deep']['summary']['cost_components_usd_range']['cache_write'][i])
        overlap=next(b for b in buckets if b['code']=='multiple_signals')
        self.assertEqual(set(overlap['examples'][0]['signals']),{'model_change','gap_over_1h'})

    def test_window_first_observation_does_not_claim_session_start(self):
        self.write([row('old',0),row('first',36000)])
        r=self.report(since=START+timedelta(seconds=36000))
        d=r['deep']['diagnosis']
        first=next(b for b in d['cache_writes']['buckets'] if b['code']=='first_observed')
        self.assertEqual(first['requests'],1)
        self.assertIsNone(first['examples'][0]['gap_seconds'])
        self.assertIn('セッション開始とは限らない',first['label'])

    def test_tied_compaction_boundary_and_request_times_stay_ambiguous(self):
        compact={'type':'system','sessionId':'session-1','timestamp':(START+timedelta(seconds=1)).isoformat(),
                 'subtype':'compact_boundary','uuid':'compact'}
        self.write([row('a',0),compact,row('b',1),row('c',2),row('d',2)])
        buckets={b['code']:b for b in self.report()['deep']['diagnosis']['cache_writes']['buckets']}
        self.assertEqual(buckets['ambiguous']['requests'],3)
        self.assertEqual(buckets['after_compaction']['requests'],0)
        self.assertTrue(all(e['context_delta'] is None for e in buckets['ambiguous']['examples']))

    def test_exact_five_minute_gap_not_tagged_over_five_minutes(self):
        self.write([row('a',0),row('b',300),row('c',601)])
        buckets={b['code']:b for b in self.report()['deep']['diagnosis']['cache_writes']['buckets']}
        self.assertEqual(buckets['unclassified']['requests'],1)
        self.assertEqual(buckets['gap_over_5m']['requests'],1)

    def test_unknown_price_and_ttl_have_token_coverage_and_preserve_range(self):
        unknown=row('unknown',0,'claude-opus-99')
        known=row('known',1)
        for r in (known,unknown):r['message']['usage'].pop('cache_creation')
        self.write([unknown,known])
        r=self.report();d=r['deep']['diagnosis'];c=d['cost_coverage']
        self.assertEqual(c['unpriced_requests'],1)
        self.assertEqual(c['unpriced_models'],['claude-opus-99'])
        self.assertEqual(c['unpriced_tokens']['cache_creation_input_tokens'],1000)
        self.assertEqual(c['unknown_ttl_write_tokens'],2000)
        self.assertEqual(c['unknown_ttl_requests'],2)
        self.assertTrue(c['cost_is_priced_subset'])
        b=next(b for b in d['cache_writes']['buckets'] if b['code']=='model_change')
        self.assertLess(b['cost_usd_range'][0],b['cost_usd_range'][1])
        for text in (markdown(r),render_html(r)):
            self.assertIn('初回診断',text)
            self.assertIn('未分類',text)
            self.assertIn('追加費用ではない',text)
            self.assertIn('未確認',text)

    def test_subagents_report_observed_models_not_universal_opus(self):
        self.write([row('a',0,'claude-opus-5'),row('b',1,'claude-sonnet-5')], 'session-1/subagents/agent-a.jsonl')
        d=self.report()['deep']['diagnosis']
        self.assertEqual({r['model'] for r in d['subagent_models']},{'claude-opus-5','claude-sonnet-5'})
        self.assertEqual(sum(r['requests'] for r in d['subagent_models']),2)
        self.assertTrue(all(r['executions']==1 for r in d['subagent_models']))

    def test_cli_initial_run_has_diagnosis_and_prices_without_flags(self):
        self.write([row('a',0)])
        with contextlib.redirect_stdout(io.StringIO()):
            rc=main(['--source',str(self.source),'--all','--output',str(self.root/'out')])
        self.assertEqual(rc,0)
        report=json.loads((self.root/'out/report.json').read_text(encoding='utf-8'))
        self.assertIn('diagnosis',report['deep'])
        self.assertEqual(report['cost']['priced_requests'],1)
        self.assertTrue(report['deep']['diagnosis']['priorities'])

    def test_explicit_lightweight_and_no_prices_remain_available(self):
        self.write([row('a',0)])
        with contextlib.redirect_stdout(io.StringIO()):
            rc=main(['--source',str(self.source),'--all','--summary-only','--no-prices','--output',str(self.root/'out')])
        self.assertEqual(rc,0)
        report=json.loads((self.root/'out/report.json').read_text(encoding='utf-8'))
        self.assertNotIn('deep',report)
        self.assertFalse(report['cost']['enabled'])

    def test_bounded_evidence_but_complete_partition_and_privacy(self):
        secret='PRIVATE_CANARY_command_path_title'
        rows=[row(str(i),i) for i in range(50)]
        for r in rows:
            r['sessionId']=secret
            r['message']['content']=[{'type':'text','text':secret}]
        self.write(rows,secret+'.jsonl')
        r=self.report();d=r['deep']['diagnosis']
        self.assertEqual(sum(b['requests'] for b in d['cache_writes']['buckets']),50)
        self.assertTrue(all(len(b['examples'])<=5 for b in d['cache_writes']['buckets']))
        self.assertNotIn(secret,json.dumps(d))
        self.assertNotIn(secret,markdown(r))
        self.assertNotIn(secret,render_html(r))


if __name__=='__main__':
    unittest.main()

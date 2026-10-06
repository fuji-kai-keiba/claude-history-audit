import contextlib
import copy
import io
import json
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
from claude_history_audit.audit import audit, load_prices
from claude_history_audit.cli import main
from claude_history_audit.report import markdown, render_html
from test_diagnosis import row, START, ROOT


def usage(write=1000, size=300000, ttl='1h'):
    u = {'input_tokens': 0, 'output_tokens': 100, 'cache_creation_input_tokens': write,
         'cache_read_input_tokens': size-write}
    if ttl:
        u['cache_creation'] = {'ephemeral_1h_input_tokens': write if ttl == '1h' else 0,
                               'ephemeral_5m_input_tokens': write if ttl == '5m' else 0}
    return u


def resume_rows(ttl='1h', gap=4000):
    rows = [row('first', 0, usage=usage(ttl=ttl))]
    for i in range(1, 5):
        rows += [row('cold'+str(i), i*gap, usage=usage(300000, ttl=ttl)),
                 row('warm'+str(i), i*gap+60, usage=usage(ttl=ttl))]
    return rows


class InsightTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root/'input'
        self.source.mkdir()
        self.prices = load_prices(ROOT/'app/claude_history_audit/reference_prices.json')

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, rows, name='a.jsonl'):
        path = self.source/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(''.join(json.dumps(r)+'\n' for r in rows), encoding='utf-8')
        return path

    def report(self, **kw):
        return audit([self.source], prices=self.prices, deep=True, **kw)[0]

    def analysis(self, **kw):
        return self.report(**kw)['deep']['diagnosis']['analysis']

    def test_resume_expiry_pattern_produces_specific_action_and_two_sided_evidence(self):
        self.write(resume_rows())
        a = self.analysis()
        c = next(c for c in a['resume_comparisons'] if c['ttl'] == '1h')
        self.assertEqual(c['status'], 'association')
        self.assertEqual(c['cold']['responses'], 4)
        self.assertEqual(c['cold']['write_p50'], 300000)
        self.assertEqual(c['cold']['full_rewrite_rate'], 1)
        self.assertEqual(c['warm']['full_rewrite_rate'], 0)
        f = next(f for f in a['findings'] if f['code'] == 'resume_1h')
        self.assertIn('休憩前', f['action'])
        self.assertIn('新しい会話', f['action'])
        self.assertEqual(f['causal_status'], 'unconfirmed')
        self.assertEqual(f['impact']['responses'], 4)
        self.assertEqual(f['impact']['component'], 'cache_write')
        self.assertGreaterEqual(len(f['evidence']), 2)
        self.assertTrue(all(c['passed'] for c in a['checks']))
        for output in (markdown(self.report()), render_html(self.report())):
            self.assertIn('休憩前', output)
            self.assertIn('全文再書込', output)
            self.assertIn('合算しない', output)

    def test_five_minute_ttl_detected_at_own_threshold(self):
        self.write(resume_rows('5m', 600))
        a = self.analysis()
        self.assertEqual(next(c for c in a['resume_comparisons'] if c['ttl']=='5m')['status'], 'association')
        self.assertNotIn('resume_1h', {f['code'] for f in a['findings']})

    def test_unknown_ttl_and_small_samples_do_not_claim_expiry(self):
        self.write(resume_rows(None))
        a = self.analysis()
        self.assertEqual(a['resume_comparisons'][0]['status'], 'not_supported')
        self.assertNotIn('resume_1h', {f['code'] for f in a['findings']})
        self.write(resume_rows()[:5])
        self.assertEqual(self.analysis()['resume_comparisons'][0]['status'], 'insufficient_data')

    def test_confounded_model_switches_are_not_expiry_evidence(self):
        rows = resume_rows()
        for i, r in enumerate(rows):
            if i % 2:
                r['message']['model'] = 'claude-opus-5'
        self.write(rows)
        a = self.analysis()
        self.assertNotIn('resume_1h', {f['code'] for f in a['findings']})
        model = next(s for s in a['gap_analysis']['signals'] if s['code']=='model_change')
        self.assertEqual(model['inclusive']['responses'], 8)
        self.assertEqual(model['exclusive_write_responses'], 4)

    def test_context_size_change_does_not_masquerade_as_resume_effect(self):
        rows = resume_rows()
        for r in rows:
            if r['message']['id'].startswith('warm'):
                r['message']['usage'] = usage(1000, 10000)
        self.write(rows)
        c = self.analysis()['resume_comparisons'][0]
        self.assertEqual(c['status'], 'insufficient_data')
        self.assertEqual(c['unmatched_cold_responses'], 4)

    def test_no_difference_is_negative_evidence_not_a_generic_expiry_warning(self):
        rows = resume_rows()
        for r in rows:
            r['message']['usage'] = usage()
        self.write(rows)
        c = self.analysis()['resume_comparisons'][0]
        self.assertEqual(c['status'], 'not_supported')

    def test_pooled_difference_without_within_execution_difference_is_not_support(self):
        # Simpson's paradox: both conditions always rewrite in A, neither in B.
        for sid, cold_count, warm_count, write in [('a',10,1,300000),('b',1,10,1000)]:
            rows=[row(sid+'first',0,usage=usage(write))]
            t=0
            for i,gap in enumerate([4000]*cold_count+[60]*warm_count):
                t+=gap
                r=row(sid+str(i),t,usage=usage(write));rows.append(r)
            for r in rows:r['sessionId']=sid
            self.write(rows,sid+'.jsonl')
        c=self.analysis()['resume_comparisons'][0]
        self.assertGreater(c['cold']['full_rewrite_rate']-c['warm']['full_rewrite_rate'],.5)
        self.assertEqual(c['mean_within_stratum_rate_difference'],0)
        self.assertEqual(c['status'],'not_supported')

    def test_empty_window_is_not_completed_diagnosis(self):
        self.write(resume_rows())
        with self.assertRaises(ValueError):
            self.analysis(since=START+timedelta(days=100))
        self.write([{'type':'user','sessionId':'only-user','timestamp':START.isoformat(),
                     'message':{'role':'user','content':'synthetic'}}])
        a=self.analysis()
        self.assertEqual(a['status'],'no_usage_data')

    def test_long_outputs_get_revision_strategy_without_claiming_document_length(self):
        rows=[row(str(i),i,usage=usage(0,1000)) for i in range(4)]
        for r in rows:r['message']['usage']['output_tokens']=10000
        self.write(rows)
        f=next(f for f in self.analysis()['findings'] if f['code']=='large_output')
        self.assertIn('推論',f['observation'])
        self.assertIn('変更した章',f['action'])
        self.assertEqual(f['impact']['component'],'output')

    def test_zero_write_population_and_exact_boundaries(self):
        rows = [row(str(i), t, usage=usage(0)) for i, t in enumerate([0,300,3900,25500,47101])]
        self.write(rows)
        a = self.analysis()
        buckets = a['gap_analysis']['buckets']
        self.assertEqual([b['responses'] for b in buckets], [1,1,1,1])
        self.assertEqual([b['write_responses'] for b in buckets], [0,0,0,0])
        self.assertEqual([b['full_rewrite_rate'] for b in buckets], [0,0,0,0])
        self.assertEqual(a['gap_analysis']['excluded']['first_observed'],1)

    def test_compaction_and_ties_are_excluded_from_resume_comparison(self):
        rows = resume_rows()
        for i in range(1,5):
            rows.append({'type':'system','sessionId':'session-1', 'subtype':'compact_boundary',
                         'timestamp':(START+timedelta(seconds=i*4000-1)).isoformat(), 'uuid':'compact'+str(i)})
        self.write(rows)
        self.assertEqual(self.analysis()['resume_comparisons'][0]['status'],'insufficient_data')
        rows.append(row('tied',4000,usage=usage()))
        self.write(rows)
        self.assertGreater(self.analysis()['gap_analysis']['excluded']['ambiguous'], 0)

    def test_impact_price_and_token_rank_are_separate_and_partial_prices_stay_unknown(self):
        rows = resume_rows()
        for r in rows:
            r['message']['model'] = 'claude-opus-99'
        self.write(rows)
        a = self.analysis()
        self.assertEqual(a['ranking_basis'], 'observed_tokens')
        f = next(f for f in a['findings'] if f['code']=='resume_1h')
        self.assertIsNone(f['impact']['cost_usd_range'])
        self.assertEqual(f['impact']['unpriced_responses'], 4)

    def test_fanout_reports_actual_mixed_models_and_parent_task(self):
        self.write([row('parent',0)])
        for i, model in enumerate(['claude-opus-5','claude-sonnet-5','claude-sonnet-5']):
            self.write([row('child'+str(i),i+1, model)],'session-1/subagents/agent-'+str(i)+'.jsonl')
        a = self.analysis()
        f = next(f for f in a['findings'] if f['code']=='subagent_fanout')
        self.assertIn('claude-opus-5 (1応答)',f['observation'])
        self.assertIn('claude-sonnet-5 (2応答)',f['observation'])
        self.assertEqual(f['impact']['responses'],3)
        self.assertIsNotNone(f['scope'])

    def test_private_canaries_never_enter_automatic_diagnosis(self):
        secret='VERY_PRIVATE_TASK_NAME_ABC'
        rows=resume_rows()
        for r in rows:
            r['sessionId']=secret
            r['message']['content']=[{'type':'text','text':secret}]
        self.write(rows,secret+'.jsonl')
        r=self.report()
        for output in (json.dumps(r),markdown(r),render_html(r)):
            self.assertNotIn(secret,output)

    def test_failed_integrity_is_nonzero_cli_even_if_report_written(self):
        self.write(resume_rows())
        r=self.report()
        r['deep']['diagnosis']['analysis']['status']='integrity_failed'
        with patch('claude_history_audit.cli.audit',return_value=(r,{})), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code=main(['--source',str(self.source),'--all','--output',str(self.root/'out')])
        self.assertEqual(code,4)


if __name__=='__main__':
    unittest.main()

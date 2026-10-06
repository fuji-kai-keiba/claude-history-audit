"""A heterogeneous synthetic corpus; these are scenarios, not validated real users."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
from claude_history_audit.audit import audit,load_prices
from claude_history_audit.report import markdown,render_html
from test_diagnosis import row,ROOT,START


def history(sid='person', count=24, size=800, output=50, model='claude-sonnet-4-6', scale=1):
    result=[]
    for n in range(count):
        o=output[n] if isinstance(output,list) else output
        c=size[n] if isinstance(size,list) else size
        r=row(sid+'-'+str(n),n+1,model,{'input_tokens':0,'output_tokens':o*scale,
                                      'cache_creation_input_tokens':0,'cache_read_input_tokens':c*scale})
        r['sessionId']=sid
        result.append(r)
    return result


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.source=self.root/'input';self.source.mkdir()
        self.prices=load_prices(ROOT/'app/claude_history_audit/reference_prices.json')

    def tearDown(self):self.tmp.cleanup()

    def write(self,rows,name='history.jsonl'):
        # The filename "history.jsonl" is the Claude prompt index, so use an ordinary file.
        name='session.jsonl' if name=='history.jsonl' else name
        path=self.source/name;path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(''.join(json.dumps(r)+'\n' for r in rows),encoding='utf-8')

    def report(self,**kwargs):return audit([self.source],prices=self.prices,deep=True,**kwargs)[0]
    def discover(self,**kwargs):return self.report(**kwargs)['deep']['diagnosis']['analysis']['discovery']

    def test_stable_small_use_has_no_automatic_difference_but_gets_review(self):
        self.write(history())
        d=self.discover()
        self.assertEqual(d['status'],'no_difference_detected')
        self.assertEqual(d['candidates'],[])
        self.assertTrue(d['review_queue'])
        self.assertEqual(d['selection']['remaining_works'],0)

    def test_stable_large_document_use_is_not_an_anomaly_by_absolute_size(self):
        self.write(history(size=900000,output=20000))
        r=self.report();a=r['deep']['diagnosis']['analysis']
        self.assertEqual(a['discovery']['candidates'],[])
        self.assertTrue(a['findings'])
        self.assertTrue(all(f['classification']=='mechanism_hypothesis_not_problem_verdict' for f in a['findings']))
        self.assertIn('異常・不要だという判定ではありません',markdown(r))

    def test_output_growth_below_old_fixed_threshold_is_discovered(self):
        self.write(history(output=[50]*23+[2000]))
        r=self.report();a=r['deep']['diagnosis']['analysis']
        c=next(c for c in a['discovery']['candidates'] if c['metric']=='output')
        self.assertEqual(c['kind'],'response_difference')
        self.assertEqual(c['baseline']['median'],50)
        self.assertEqual(c['observed_max'],2000)
        self.assertEqual(c['observed_responses'],1)
        self.assertGreaterEqual(len(c['evidence']),2)
        self.assertNotIn('large_output',{f['code'] for f in a['findings']})

    def test_different_models_are_not_pooled_into_false_anomalies(self):
        self.write(history('a',output=50)+history('b',output=20000,model='claude-opus-5'))
        self.assertEqual(self.discover()['candidates'],[])

    def test_parent_and_child_baselines_remain_separate(self):
        self.write(history('parent',output=50),'parent.jsonl')
        self.write(history('parent',output=20000),'parent/subagents/agent-a.jsonl')
        # Give child messages distinct IDs while retaining its parent's session ID.
        path=self.source/'parent/subagents/agent-a.jsonl'
        rows=[json.loads(s) for s in path.read_text().splitlines()]
        for r in rows:r['message']['id']='child-'+r['message']['id']
        self.write(rows,'parent/subagents/agent-a.jsonl')
        d=self.discover()
        self.assertEqual({p['role'] for p in d['profile']},{'main','subagent'})
        self.assertEqual(d['candidates'],[])

    def test_workload_hints_separate_document_and_development_use(self):
        rows=[]
        for sid,topic,size in [('docs','会議資料',2000),('dev','実装',50)]:
            rows.append({'type':'user','sessionId':sid,'uuid':'prompt-'+sid,'timestamp':START.isoformat(),
                         'message':{'role':'user','content':topic}})
            rows.extend(history(sid,output=size))
        self.write(rows)
        d=self.discover()
        self.assertEqual({p['workload_hint'] for p in d['profile']},{'社内会議','開発'})
        self.assertEqual(d['candidates'],[])

    def test_output_reference_also_accounts_for_input_size(self):
        self.write(history('short',size=200,output=50)+history('long',size=200000,output=20000))
        d=self.discover()
        self.assertFalse(any(c['metric']=='output' for c in d['candidates']))
        output_baselines=[b for b in d['response_baselines'] if b['metric']=='output']
        self.assertEqual(len({b['input_size_band'] for b in output_baselines}),2)

    def test_many_small_calls_can_make_one_work_unusual(self):
        rows=[]
        for i in range(8):rows.extend(history('work-'+str(i),count=150 if i==7 else 12))
        self.write(rows)
        c=next(c for c in self.discover()['candidates'] if c['kind']=='work_difference')
        self.assertEqual(c['observed_responses'],150)
        self.assertEqual(c['baseline']['reference_works'],8)
        self.assertGreater(c['baseline']['observed_value'],c['baseline']['threshold'])

    def test_sustained_change_is_found_even_when_global_distribution_is_broad(self):
        self.write(history(size=[800]*12+[8000]*12))
        d=self.discover()
        c=next(c for c in d['candidates'] if c['kind']=='within_execution_change' and c['metric']=='context')
        self.assertEqual(c['baseline']['median'],800)
        self.assertEqual(c['baseline']['after_median'],8000)

    def test_tied_temporal_boundary_does_not_claim_a_change(self):
        rows=history(size=[800]*12+[8000]*12)
        for r in rows:r['timestamp']=START.isoformat()
        self.write(rows)
        d=self.discover()
        self.assertEqual(d['tied_change_boundaries'],1)
        self.assertFalse(any(c['kind']=='within_execution_change' for c in d['candidates']))

    def test_small_history_stays_insufficient_not_normal(self):
        self.write(history(count=3,output=[1,2,10000]))
        d=self.discover()
        self.assertEqual(d['status'],'insufficient_comparison_data')
        self.assertTrue(d['review_queue'])

    def test_missing_usage_is_not_a_zero_baseline(self):
        rows=history()
        for r in rows:r['message']['usage']={'input_tokens':10}
        self.write(rows)
        d=self.discover()
        self.assertEqual(d['status'],'insufficient_comparison_data')
        self.assertEqual(d['eligible_responses'],0)
        self.assertEqual(d['excluded_from_response_baselines']['incomplete_usage'],24)
        self.assertIsNone(d['profile'][0]['output']['p50'])

    def test_unknown_price_keeps_personal_comparison_and_uses_token_ranking(self):
        self.write(history(model='claude-opus-99',output=[50]*23+[2000]))
        d=self.discover()
        self.assertEqual(d['selection']['basis'],'observed_tokens')
        self.assertIsNone(d['selection']['selected']['cost_usd_range'])
        self.assertTrue(any(c['metric']=='output' for c in d['candidates']))

    def test_all_zero_use_remains_finite_and_has_no_spurious_difference(self):
        self.write(history(size=0,output=0))
        d=self.discover()
        self.assertEqual(d['candidates'],[])
        self.assertIsNone(d['selection']['achieved_share'])
        self.assertNotIn('NaN',json.dumps(d,allow_nan=False))

    def test_relative_results_survive_large_scale_changes(self):
        self.write(history(output=[50]*23+[2000]))
        a=self.discover()
        self.write(history(output=[50]*23+[2000],scale=64))
        b=self.discover()
        signature=lambda d:sorted((c['kind'],c['metric'],c['observed_responses']) for c in d['candidates'])
        self.assertEqual(signature(a),signature(b))

    def test_name_path_and_identity_changes_do_not_change_diagnosis(self):
        self.write(history('PRIVATE_WINDOWS_USER',output=[50]*23+[2000]),'PRIVATE_WINDOWS_PATH.jsonl')
        r=self.report(identity_key=b'a'*32)
        d=r['deep']['diagnosis']['analysis']['discovery']
        other=self.discover(identity_key=b'b'*32)
        self.assertEqual(d['status'],other['status'])
        self.assertEqual([(c['kind'],c['metric']) for c in d['candidates']],[(c['kind'],c['metric']) for c in other['candidates']])
        for text in (json.dumps(r),markdown(r),render_html(r)):
            self.assertNotIn('PRIVATE_WINDOWS',text)

    def test_coverage_partition_reports_unselected_work_without_claiming_eighty_percent(self):
        rows=[]
        for i in range(120):rows.extend(history('work-'+str(i),count=1))
        self.write(rows)
        r=self.report();d=r['deep']['diagnosis']['analysis']['discovery'];s=d['selection']
        self.assertGreater(s['remaining_works'],0)
        self.assertLess(s['achieved_share'],s['target_share'])
        self.assertEqual(s['selected_works']+s['remaining_works'],120)
        self.assertEqual(s['selected']['requests']+s['remaining']['requests'],120)
        for field in r['totals']:
            self.assertEqual(s['selected']['tokens'][field]+s['remaining']['tokens'][field],r['totals'][field])
        self.assertTrue(all(d['checks'].values()))
        self.assertIn('未選択',markdown(r))

    def test_new_work_evidence_requires_questions_beyond_known_rules(self):
        self.write(history())
        d=self.discover()
        works=[q for q in d['review_queue'] if q['kind']=='work_review']
        self.assertTrue(works)
        self.assertTrue(all(len(q['discovery_questions'])==3 for q in works))


if __name__=='__main__':unittest.main()

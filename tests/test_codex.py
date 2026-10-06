"""Synthetic native rollout fixtures; never read the developer's history."""
import json
import itertools
import os
import sys
import tempfile
import unittest
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'app'))
from claude_history_audit.audit import audit, load_prices, read_records
from claude_history_audit.codex import records, poll_call, local_sources, excerpt
from claude_history_audit.report import write_reports
from claude_history_audit.evidence import inspect_item, finalize, load

ROOT = Path(__file__).resolve().parents[1]
START = datetime(2026, 10, 1, tzinfo=timezone.utc)


def row(kind, payload, t=0):
    return {'type':kind, 'timestamp':(START+timedelta(seconds=t)).isoformat(), 'payload':payload}


def usage(inp=1000, cached=800, write=0, out=100):
    return dict(input_tokens=inp, cached_input_tokens=cached, cache_write_input_tokens=write,
                output_tokens=out, reasoning_output_tokens=40, total_tokens=inp+out)


def header(sid='synthetic', parent=None, model='gpt-6-astra'):
    source = {'subagent':{'thread_spawn':{'parent_thread_id':parent}}} if parent else 'vscode'
    return [row('session_meta',dict(id=sid,source=source,cwd='PRIVATE_PATH')),
            row('turn_context',dict(model=model))]


def response(rid='r1', t=1, sid='synthetic', u=None):
    return row('token_usage_record',dict(thread_id=sid,response_id=rid,usage=usage() if u is None else u),t)


def notification(total, last, t):
    return row('event_msg',dict(type='token_count',info=dict(total_token_usage=total,last_token_usage=last)),t)


def polling_history():
    rows=header()
    rows.append(row('response_item',dict(type='message',role='user',content=[dict(type='input_text',text='PRIVATE_CANARY build test')])) )
    for i in range(3):
        rows += [row('response_item',dict(type='custom_tool_call',name='functions.exec',call_id='call-'+str(i),
                     input='text(await tools.write_stdin({session_id: 7, chars: "", yield_time_ms: 1000}));'),i*10+1),
                 response('r'+str(i),i*10+2),
                 row('response_item',dict(type='custom_tool_call_output',call_id='call-'+str(i),output=json.dumps([
                     dict(type='text',text='still running'),dict(type='image',data='BASE64_PRIVATE'*1000)])),i*10+3)]
    return rows


class CodexTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.source=self.root/'input';self.source.mkdir()
        self.prices=load_prices(ROOT/'app/claude_history_audit/reference_prices.json')

    def write(self, rows, name='a.jsonl'):
        p=self.source/name;p.write_text(''.join(json.dumps(r)+'\n' for r in rows),encoding='utf-8');return p

    def report(self, **kw):
        return audit([self.source],deep=True,prices=self.prices,**kw)[0]

    def test_response_usage_wins_over_notifications_and_copies(self):
        rows=header()+[response(u=usage(write=100)),notification(usage(),usage(),2)]
        self.write(rows);self.write(rows,'copy.jsonl')
        r=self.report()
        self.assertEqual(r['coverage']['unique_requests'],1)
        self.assertEqual(r['totals'],dict(input_tokens=100,cache_read_input_tokens=800,cache_creation_input_tokens=100,output_tokens=100))
        self.assertEqual(r['requests'][0]['reasoning_output_tokens'],40)
        self.assertAlmostEqual(r['cost']['usd_range'][0],.00805)
        self.assertEqual(r['deep']['summary']['tokens'],r['totals'])

    def test_legacy_repeat_and_compaction_reset_are_not_cumulative_sum(self):
        u=usage();u.pop('cache_write_input_tokens')
        total={k:v*2 for k,v in u.items()}
        rows=header()+[notification(u,u,1),notification(u,u,2),notification(total,u,3),row('compacted',{},4),notification(u,u,5)]
        self.write(rows)
        r=self.report()
        self.assertEqual(r['coverage']['unique_requests'],3)
        self.assertEqual(r['metrics']['input_total'],3000)
        self.assertEqual(r['cost']['unpriced_requests'],3)
        self.assertEqual(r['coverage']['codex_legacy_notifications_repeated'],1)
        self.assertEqual(r['sessions'][0]['compactions'],1)

    def test_legacy_partial_prefix_is_not_guessed(self):
        u=usage();twice={k:v*2 for k,v in u.items()};three={k:v*3 for k,v in u.items()}
        self.write(header()+[notification(twice,u,1),notification(three,u,2)])
        r=self.report();self.assertEqual(r['coverage']['unique_requests'],1)
        self.assertEqual(r['coverage']['codex_legacy_ambiguous'],1)

    def test_legacy_reset_with_identical_counters_keeps_both_responses(self):
        u=usage()
        # Even tied timestamps across a real compaction are separate responses.
        rows=header()+[notification(u,u,1),row('compacted',{},1),notification(u,u,1),notification(u,u,2)]
        self.write(rows);self.write(rows,'copy.jsonl')
        r=self.report()
        self.assertEqual(r['coverage']['unique_requests'],2)
        self.assertEqual(r['metrics']['input_total'],2000)

    def test_legacy_inherited_usage_has_no_safe_owner_and_is_excluded(self):
        u=usage();total={k:v*2 for k,v in u.items()}
        self.write(header('parent')+[notification(u,u,1)])
        rows=header('child','parent')+[row('session_meta',dict(id='parent',source='vscode')),notification(u,u,1),notification(total,u,2)]
        self.write(rows,'child.jsonl')
        r=self.report()
        self.assertEqual(r['coverage']['unique_requests'],1)
        self.assertEqual(r['metrics']['input_total'],1000)
        self.assertEqual(r['coverage']['codex_legacy_fork_usage_ambiguous'],2)

    def test_child_keeps_first_header_and_explicit_parent(self):
        self.write(header('parent')+[response('parent-r',sid='parent')])
        child=header('child','parent')
        child.insert(1,row('session_meta',dict(id='parent',source='vscode')))
        child += [response('foreign',sid='parent'),row('turn_context',dict(model='gpt-6.1-sol'),2),response('child-r',3,'child')]
        self.write(child,'child.jsonl')
        r=self.report()
        self.assertEqual(r['coverage']['unique_requests'],2)
        self.assertEqual(sum(e['parent'] is not None for e in r['deep']['executions']),1)
        self.assertEqual(len(r['deep']['groups']),1)
        self.assertEqual(r['coverage']['codex_foreign_response_ignored'],1)
        self.assertEqual({x['model'] for x in r['requests']},{'gpt-6-astra','gpt-6.1-sol'})

    def test_missing_cache_and_unknown_model_stay_unpriced(self):
        u=usage();u.pop('cached_input_tokens')
        self.write(header()+[response(u=u),row('turn_context',dict(model='PRIVATE_MODEL'),2),response('r2',3)])
        r=self.report();self.assertEqual(r['cost']['unpriced_requests'],2)
        self.assertFalse(r['requests'][0]['complete'])
        self.assertEqual(r['requests'][1]['model'],'unknown')

    def test_malformed_usage_is_excluded_and_counted(self):
        bad=[]
        for key,value in [('input_tokens',-1),('output_tokens',True),('cached_input_tokens',1001),('reasoning_output_tokens',101),('total_tokens',5)]:
            u=usage();u[key]=value;bad.append(response('bad-'+key,len(bad)+2,u=u))
        bad.append(row('token_usage_record',dict(thread_id='synthetic',response_id='missing'),9))
        self.write(header()+[response()]+bad)
        r=self.report();self.assertEqual(r['coverage']['unique_requests'],1)
        self.assertEqual(r['coverage']['codex_invalid_usage'],6)

    def test_long_context_price_boundary_and_components_reconcile(self):
        self.write(header()+[response('short',1,u=usage(272000,200000,100)),response('long',2,u=usage(272001,200000,100))])
        r=self.report();a,b=r['requests']
        self.assertAlmostEqual(a['cost_usd_range'][0],(71900*10+200000+100*12.5+100*50)/1e6)
        self.assertAlmostEqual(b['cost_usd_range'][0],((71901*10+200000+100*12.5)*2+100*50*1.5)/1e6)
        parts=r['deep']['summary']['cost_components_usd_range']
        self.assertAlmostEqual(sum(v[0] for v in parts.values()),r['cost']['usd_range'][0])

    def test_nonstandard_speed_is_not_standard_cost(self):
        rows=header();rows[-1]['payload']['service_tier']='priority';self.write(rows+[response()])
        self.assertIsNone(self.report()['cost']['usd_range'])

    def test_conflicting_duplicate_remains_unpriced_after_good_copy(self):
        self.write(header()+[response(),response(u=usage(inp=1200)),response()])
        r=self.report()
        self.assertEqual(r['coverage']['unique_requests'],1)
        self.assertEqual(r['cost']['unpriced_requests'],1)
        self.assertFalse(r['requests'][0]['complete'])

    def test_complete_and_partial_copies_never_invent_input(self):
        full=usage();partial=dict(full);partial.pop('cached_input_tokens')
        for order in itertools.permutations([full,partial,full]):
            with self.subTest(order=order):
                self.write(header()+[response(u=u) for u in order])
                r=self.report()
                self.assertEqual(r['coverage']['unique_requests'],1)
                self.assertEqual(r['metrics']['input_total'],1000)
                self.assertEqual(r['totals']['cache_read_input_tokens'],800)
                self.assertEqual(r['cost']['unpriced_requests'],1)

    def test_conflicting_complete_copies_select_one_whole_record(self):
        a,b=usage(cached=800),usage(cached=100,write=300)
        for order in ((a,b),(b,a)):
            self.write(header()+[response(u=u) for u in order])
            r=self.report()
            self.assertEqual(r['metrics']['input_total'],1000)
            observed=(r['totals']['input_tokens'],r['totals']['cache_read_input_tokens'],r['totals']['cache_creation_input_tokens'])
            self.assertEqual(observed,(600,100,300))
            self.assertEqual(r['cost']['unpriced_requests'],1)

    def test_default_tier_is_standard_but_fast_is_not(self):
        rows=header();rows[-1]['payload']['service_tier']='default'
        rows += [response(),row('turn_context',dict(model='gpt-6-astra',speed='fast'),2),response('fast',3)]
        self.write(rows);r=self.report()
        self.assertEqual(r['cost']['priced_requests'],1)
        self.assertEqual(r['cost']['unpriced_requests'],1)

    def test_window_filter_keeps_metadata_from_before_window(self):
        self.write(header()+[response('before',1),response('inside',2),response('end',3)])
        r=self.report(since=START+timedelta(seconds=2),until=START+timedelta(seconds=3))
        self.assertEqual(r['coverage']['unique_requests'],1)
        self.assertEqual(r['requests'][0]['model'],'gpt-6-astra')

    def test_poll_only_recognizer_rejects_work_or_dynamic_code(self):
        self.assertTrue(poll_call('functions.exec','text(await tools.write_stdin({session_id:7, chars:""}));'))
        self.assertTrue(poll_call('clock.sleep','{"duration_ms":1000}'))
        self.assertTrue(poll_call('functions.wait','{"cell_id":"cell1"}'))
        self.assertTrue(poll_call('write_stdin',{'session_id':7}))
        for name,args in [('write_stdin',{'session_id':7,'chars':'y'}),('wait',{'cell_id':'cell1','terminate':True}),
                          ('custom.exec','text(await tools.write_stdin({session_id:7}));'),
                          ('exec','text(await tools.write_stdin({session_id:variable}));'),
                          ('exec','text(await tools.write_stdin({session_id:7})); await work();'),
                          ('exec','"text(await tools.write_stdin({session_id:7}));"')]:
            self.assertFalse(poll_call(name,args),(name,args))

    def test_polling_detection_and_native_evidence_review(self):
        source=self.write(polling_history());original=source.read_bytes()
        r,local=audit([source],deep=True,prices=self.prices)
        a=r['deep']['diagnosis']['analysis'];finding=next(f for f in a['findings'] if f['code']=='polling')
        self.assertEqual(finding['impact']['responses'],3)
        self.assertIn('not_incremental',finding['impact']['measurement'])
        self.assertTrue(all(c['status']=='not_applicable' for c in a['resume_comparisons']))
        out=write_reports(self.root/'out',r,local)
        notes=load(out/'review-notes.private.json')
        with self.assertRaises(ValueError):finalize(out,notes)
        for item in notes['items']:
            packet=inspect_item(out,item['id'])
            self.assertNotIn('BASE64_PRIVATE',json.dumps(packet))
            self.assertTrue(all(e['available'] for e in packet['excerpts']))
            item.update(status='supported_hypothesis',evidence=[e['focus'] for e in packet['excerpts']],
                        observation='合成の実行終了を待つ確認が3回ある',interpretation='監視頻度を比較できる候補',
                        alternatives='実行終了を速やかに検知するために必要',action='合成実行の終了通知と比較する',validation='完了検知時間と総使用量を比較')
            if 'open_review' in item:
                item['open_review']=dict(purpose='合成テストの完了待ち',necessary_work='完了確認',unlisted_issues='抜粋では他の問題なし',missing_information='実行に必要な時間')
        self.assertIn(finalize(out,notes)['status'],('reviewed','reviewed_with_limits'))
        for name in ('report.json','report.html','summary.md','review-plan.json'):
            content=(out/name).read_text()
            for private in ('PRIVATE_CANARY','PRIVATE_PATH','BASE64_PRIVATE','session_id: 7'):
                self.assertNotIn(private,content)
        self.assertEqual(source.read_bytes(),original)

    def test_images_do_not_count_as_text(self):
        from claude_history_audit.deep import text_size
        from claude_history_audit.codex import text_blocks
        value=json.dumps([dict(type='text',text='abc'),dict(type='image',data='x'*100000)])
        self.assertEqual(text_size(text_blocks(value)),(3,1))
        self.assertEqual(excerpt(row('response_item',dict(type='custom_tool_call_output',output=value))),'abc\n[非テキスト要素]')

    def test_mixed_work_is_not_polling(self):
        rows=polling_history()
        rows.insert(4,row('response_item',dict(type='function_call',name='functions.exec_command',call_id='work',arguments='{"cmd":"build"}'),1))
        self.write(rows)
        r=self.report();self.assertEqual(sum(x['polling'] for x in r['requests']),2)
        self.assertNotIn('polling',{f['code'] for f in r['deep']['diagnosis']['analysis']['findings']})

    def test_provider_filter_and_collision_do_not_combine_usage(self):
        self.write(header()+[response()])
        self.write([{'type':'assistant','timestamp':START.isoformat(),'sessionId':'codex:synthetic','_codex':{'complete':True},
                     'message':{'id':'codex:r1','model':'claude-sonnet-4-6','usage':dict(input_tokens=1,output_tokens=1,cache_read_input_tokens=0,cache_creation_input_tokens=0)}}],'claude.jsonl')
        r=self.report();self.assertEqual(r['coverage']['unique_requests'],2);self.assertEqual(r['coverage']['sessions'],2)
        self.assertEqual(self.report(provider='claude')['providers'],['claude'])
        self.assertEqual(self.report(provider='codex')['providers'],['codex'])

    def test_discovery_reads_both_codex_directories(self):
        for name in ('sessions','archived_sessions'):(self.root/name).mkdir()
        with patch.dict(os.environ,{'CODEX_HOME':str(self.root)}):
            self.assertEqual(set(local_sources()),{self.root/'sessions',self.root/'archived_sessions'})

    def test_original_line_numbers_are_preserved(self):
        p=self.write(header()+[response()]);stats=Counter()
        rows=list(records(p,stats,read_records))
        self.assertEqual(rows[0][0],3)
        self.assertEqual(rows[0][1]['message']['id'],'codex:r1')


if __name__=='__main__':unittest.main()

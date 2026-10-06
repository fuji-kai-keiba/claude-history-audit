import contextlib
import copy
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
from claude_history_audit.audit import audit, load_prices, TOKEN_FIELDS
from claude_history_audit.cli import main
from claude_history_audit.report import write_reports
from test_audit import NOW, response, tool

ROOT = Path(__file__).resolve().parents[1]


def stamp(second):
    return '2026-10-04T10:00:' + str(second).zfill(2) + 'Z'


def result(tid, second=1, error=False, text='SENSITIVE_RESULT', session='session-1'):
    return {'type': 'user', 'timestamp': stamp(second), 'sessionId': session,
            'message': {'content': [{'type': 'tool_result', 'tool_use_id': tid, 'is_error': error, 'content': text}]}}


class DeepTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / 'input'
        self.source.mkdir()
        self.prices = load_prices(ROOT / 'app/claude_history_audit/reference_prices.json')

    def tearDown(self):
        self.temp.cleanup()

    def write(self, rows, name='one.jsonl'):
        path = self.source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(''.join(json.dumps(r) + '\n' for r in rows))

    def run_audit(self, **kwargs):
        return audit([self.source], deep=True, now=NOW, **kwargs)[0]

    def test_deep_preserves_dedup_totals_and_groups(self):
        rows = [response(), response('second')]
        self.write(rows + rows)
        plain = audit([self.source], now=NOW)[0]
        deep = self.run_audit()
        self.assertNotIn('deep', plain)
        for key in ('totals', 'metrics', 'coverage', 'cost'):
            self.assertEqual(plain[key], deep[key])
        for f in TOKEN_FIELDS:
            self.assertEqual(sum(g['tokens'][f] for g in deep['deep']['groups']), plain['totals'][f])
        self.assertEqual(deep['deep']['summary']['requests'], 2)

    def test_parent_child_and_orphans_do_not_double_count(self):
        self.write([response(session='parent')], 'parent.jsonl')
        self.write([response('child', session='parent')], 'parent/subagents/agent-a.jsonl')
        self.write([response('other-id-child', session='own-id')], 'parent/subagents/agent-b.jsonl')
        self.write([response('orphan', session='unknown')], 'unknown/subagents/agent-c.jsonl')
        d = self.run_audit()['deep']
        self.assertEqual(d['coverage']['main_executions'], 1)
        self.assertEqual(d['coverage']['subagent_executions'], 3)
        self.assertEqual(d['coverage']['linked_subagents'], 2)
        self.assertEqual(d['coverage']['unlinked_subagents'], 1)
        self.assertEqual(sorted(g['requests'] for g in d['groups']), [1, 3])

    def test_conflicting_parent_evidence_remains_unlinked(self):
        self.write([response(session='a')], 'a.jsonl')
        self.write([response('b', session='b')], 'b.jsonl')
        self.write([response('child', session='a')], 'b/subagents/agent-a.jsonl')
        d = self.run_audit()['deep']
        child = next(e for e in d['executions'] if e['subagent'])
        self.assertIsNone(child['parent'])
        self.assertTrue(child['parent_ambiguous'])

    def test_copied_response_across_executions_flagged_once(self):
        row = response()
        self.write([row], 'root.jsonl')
        self.write([row], 'session-1/subagents/agent-a.jsonl')
        d = self.run_audit()['deep']
        self.assertEqual(d['coverage']['requests_seen_in_multiple_executions'], 1)
        self.assertEqual(sum(g['requests'] for g in d['groups']), 1)

    def test_fractional_timestamps_sorted_numerically(self):
        a = response('a', timestamp='2026-10-04T10:00:00Z')
        b = response('b', timestamp='2026-10-04T10:00:00.100Z')
        b['message']['usage']['cache_read_input_tokens'] += 5000
        self.write([b, a])
        t = self.run_audit()['deep']['executions'][0]['trajectory']
        self.assertEqual(t[0]['timestamp'], a['timestamp'])
        self.assertEqual(t[1]['delta_tokens'], 5000)
        self.assertEqual(t[1]['gap_seconds'], .1)

    def test_equal_times_and_incomplete_counters_do_not_claim_delta(self):
        self.write([response('a'), response('b'), response('c', timestamp=stamp(1), usage={'input_tokens': 1})])
        t = self.run_audit()['deep']['executions'][0]['trajectory']
        self.assertTrue(t[0]['order_ambiguous'])
        self.assertTrue(t[1]['order_ambiguous'])
        self.assertTrue(all(p['delta_tokens'] is None for p in t))

    def test_tool_sizes_dedup_and_neighbors_are_observations(self):
        a = response(content=[tool('t1')])
        b = response('b', timestamp=stamp(2))
        b['message']['usage']['input_tokens'] += 123
        r = result('t1', text=[{'type': 'text', 'text': 'あ'}, {'type': 'image', 'source': {'data': 'PRIVATE'}}])
        self.write([a, r, r, b])
        d = self.run_audit()['deep']
        self.assertEqual(d['coverage']['matched_tool_results'], 1)
        item = d['largest_tool_results'][0]
        self.assertEqual(item['text_bytes'], 3)
        self.assertEqual(item['nontext_blocks'], 1)
        self.assertEqual(item['neighbor_context_delta'], 123)
        self.assertEqual(d['tool_results_by_type'][0]['text_bytes'], 3)

    def test_retry_exact_arguments_with_interval_union(self):
        self.write([response(content=[tool('a'), tool('b', path='/other.pdf')]),
                    result('a', error=True), result('b', error=True),
                    response('retry', timestamp=stamp(2), content=[tool('c'), tool('d', path='/other.pdf')]),
                    result('c', second=3), result('d', second=3)])
        d = self.run_audit(prices=self.prices)['deep']
        self.assertEqual(len(d['retries']), 2)
        self.assertTrue(all(r['outcome'] == 'success' for r in d['retries']))
        self.assertEqual(sum(r['observed_interval']['requests'] for r in d['retries']), 2)
        self.assertEqual(d['retry_observed_union']['requests'], 1)
        self.assertEqual(d['retry_observed_union']['priced_requests'], 1)

    def test_changed_arguments_other_execution_success_and_late_calls_are_not_retries(self):
        self.write([response(content=[tool('a')]), result('a', error=True),
                    response('changed', timestamp=stamp(2), content=[tool('b', path='/changed')]),
                    response('other', session='other', timestamp=stamp(3), content=[tool('c')]),
                    response('late', timestamp='2026-10-04T10:10:00Z', content=[tool('d')]),
                    result('d', second=4), # invalid result before the call
                    response('late2', timestamp='2026-10-04T10:10:02Z', content=[tool('e')])])
        d = self.run_audit()['deep']
        self.assertEqual(d['retries'], [])
        self.assertEqual(d['coverage']['unmatched_tool_results'], 1)

    def test_missing_conflicting_and_unmatched_results(self):
        missing = result('')
        self.write([response(content=[tool('a'), tool('missing')]),
                    result('a', error=True), result('a', error=False), result('unmatched'), missing,
                    response('retry', timestamp=stamp(2), content=[tool('retry')])])
        d = self.run_audit()['deep']
        self.assertEqual(d['retries'], [])
        self.assertEqual(d['coverage']['conflicting_tool_results'], 1)
        self.assertEqual(d['coverage']['unidentified_tool_results'], 1)
        self.assertEqual(d['coverage']['unmatched_tool_results'], 1)
        self.assertEqual(d['coverage']['calls_without_result'], 2)

    def test_compaction_context_and_tied_boundaries(self):
        a = response()
        b = response('b', timestamp=stamp(2))
        b['message']['usage']['cache_read_input_tokens'] = 1000
        compact = {'type':'system','sessionId':'session-1','timestamp':stamp(1),'subtype':'compact_boundary','uuid':'c1'}
        tied = dict(compact, timestamp=stamp(2), uuid='c2')
        self.write([a, compact, compact, b, tied])
        d = self.run_audit()['deep']
        self.assertEqual(len(d['compactions']), 2)
        self.assertEqual(d['compactions'][0]['context_delta'], -8000)
        self.assertTrue(d['compactions'][1]['order_ambiguous'])
        self.assertIsNone(d['compactions'][1]['context_delta'])

    def test_reference_opus_price_components_unknown_ttl_and_coverage(self):
        row = response()
        row['message']['model'] = 'claude-opus-5'
        row['message']['usage'].pop('cache_creation')
        self.write([row])
        d = self.run_audit(prices=self.prices)['deep']
        self.assertEqual(d['ranking_basis'], 'api_equivalent_usd_upper')
        parts, total = d['summary']['cost_components_usd_range'], d['summary']['cost_usd_range']
        self.assertAlmostEqual(parts['cache_write'][0], .00625)
        self.assertAlmostEqual(parts['cache_write'][1], .01)
        for i in (0,1):
            self.assertAlmostEqual(sum(v[i] for v in parts.values()), total[i])
        bad = copy.deepcopy(row)
        bad['message']['id'] = 'unknown'
        bad['message']['model'] = 'claude-opus-99'
        self.write([bad], 'unknown.jsonl')
        d = self.run_audit(prices=self.prices)['deep']
        self.assertEqual(d['ranking_basis'], 'input_tokens')
        self.assertEqual(d['summary']['unpriced_reasons'], {'unknown_model':1})
        self.assertEqual(d['summary']['unpriced_requests'], 1)
        self.assertEqual(d['summary']['cost_usd_range'], total)

    def test_nonstandard_and_incomplete_usage_never_priced(self):
        fast = response('fast')
        fast['message']['usage']['speed'] = 'fast'
        partial = response('partial', timestamp=stamp(2), usage={'input_tokens':1})
        self.write([fast, partial])
        d = self.run_audit(prices=self.prices)['deep']
        self.assertEqual(d['summary']['unpriced_reasons'], {'nonstandard_pricing':1,'incomplete_usage':1})
        self.assertIsNone(d['summary']['cost_usd_range'])

    def test_private_contents_commands_ids_and_paths_absent_from_outputs(self):
        secret = 'PRIVATE_CANARY_<script>alert(1)</script>'
        self.write([response(mid=secret, session=secret, content=[tool(secret, name='mcp__'+secret, path=secret, command=secret)]),
                    result(secret, session=secret, text=secret)])
        report, mapping = audit([self.source], deep=True, prices=self.prices)
        dest = write_reports(self.root/'report', report, mapping)
        for filename in ['report.json','report.html','summary.md']:
            text = (dest/filename).read_text(encoding='utf-8')
            for value in [secret, str(self.source), 'PRIVATE_CANARY', 'SENSITIVE_RESULT']:
                self.assertNotIn(value, text)
        self.assertIn(secret, json.dumps(mapping))
        self.assertIn('深掘り', (dest/'report.html').read_text(encoding='utf-8'))

    def test_explicit_timezone_window_has_exclusive_end(self):
        self.write([response('before', timestamp='2026-09-05T14:59:59Z'),
                    response('start', timestamp='2026-09-05T15:00:00Z'),
                    response('end', timestamp='2026-09-06T15:00:00Z')])
        dest = self.root/'report'
        with contextlib.redirect_stdout(io.StringIO()):
            rc = main(['--source',str(self.source),'--since-at','2026-09-06T00:00:00+09:00',
                       '--until-at','2026-09-07T00:00:00+09:00','--deep','--reference-prices','--output',str(dest)])
        self.assertEqual(rc, 0)
        r = json.loads((dest/'report.json').read_text(encoding='utf-8'))
        self.assertEqual(r['coverage']['unique_requests'],1)
        self.assertEqual(r['window']['since'],'2026-09-05T15:00:00Z')
        self.assertEqual(r['cost']['priced_requests'],1)
        self.assertIn('deep',r)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main(['--since-at','2026-09-06T00:00:00'])

    def test_simultaneous_previous_calls_never_depend_on_identity_salt(self):
        self.write([response(content=[tool('a'), tool('b')]),
                    result('a', error=True), result('b', error=False),
                    response('retry', timestamp=stamp(2), content=[tool('c')]), result('c', second=3)])
        for byte in range(12):
            d = self.run_audit(identity_key=bytes([byte])*32)['deep']
            self.assertEqual(d['retries'], [])
            self.assertEqual(d['coverage']['ambiguous_tool_call_order'], 2)

    def test_simultaneous_retry_calls_are_ambiguous(self):
        self.write([response(content=[tool('a')]), result('a', error=True),
                    response('retry', timestamp=stamp(2), content=[tool('b'), tool('c')]),
                    result('b', second=3), result('c', second=3)])
        d = self.run_audit()['deep']
        self.assertEqual(d['retries'], [])
        self.assertEqual(d['coverage']['ambiguous_tool_call_order'], 2)

    def test_maximum_result_size_keeps_its_own_time_and_evidence(self):
        self.write([response(content=[tool('a')]), result('a', text='x'),
                    result('a', second=2, text='x'*100),
                    response('after', timestamp=stamp(3))])
        r = self.run_audit()['deep']['largest_tool_results'][0]
        self.assertEqual(r['text_bytes'], 100)
        self.assertEqual(r['result_evidence']['line'], 3)
        self.assertEqual(r['timestamp'], stamp(2))
        self.assertEqual(r['after_evidence']['line'], 4)

    def test_reference_price_copies_match(self):
        self.assertEqual(self.prices,load_prices(ROOT/'docs/prices.example.json'))


if __name__ == '__main__':
    unittest.main()

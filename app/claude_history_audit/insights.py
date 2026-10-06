"""Deterministic first-pass diagnosis. No model calls and no transcript text export."""
import math
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict

from .audit import TOKEN_FIELDS, parse_time, percentile
from .deep import aggregate, context

GAPS = [('within_5m', '5分以内'), ('5m_to_1h', '5分超〜1時間'),
        ('1h_to_6h', '1時間超〜6時間'), ('over_6h', '6時間超')]
FULL_REWRITE = .9
MIN_COMPARISON = 3


def ratio(a, b):
    return a / b if b else None


def samples(rows, limit=3, field='cache_creation_input_tokens'):
    return [r['evidence'] for r in sorted(rows, key=lambda r: (-r['usage'][field], r['timestamp'], r['id']))[:limit]]


def unique_evidence(items):
    result, seen = [], set()
    for e in items:
        if e and (e['file'], e['line']) not in seen:
            result.append(e)
            seen.add((e['file'], e['line']))
    return result


def transitions(by_session, compactions, ambiguous_ids):
    boundaries = defaultdict(list)
    for c in compactions:
        boundaries[c['session']].append(parse_time(c['timestamp']))
    for times in boundaries.values():
        times.sort()
    events, excluded = [], Counter()
    for sid, rows in by_session.items():
        times = [parse_time(r['timestamp']) for r in rows]
        counts = Counter(times)
        bs = boundaries[sid]
        for i, r in enumerate(rows):
            if not i:
                excluded['first_observed'] += 1
                continue
            p, pt, ts = rows[i-1], times[i-1], times[i]
            if (not r['complete'] or not p['complete'] or r['model'] == 'unknown' or p['model'] == 'unknown'
                    or counts[pt] > 1 or counts[ts] > 1 or r['id'] in ambiguous_ids or p['id'] in ambiguous_ids
                    or any(bisect_left(bs, t) != bisect_right(bs, t) for t in (pt, ts))):
                excluded['ambiguous'] += 1
                continue
            gap = (ts-pt).total_seconds()
            bucket = 'within_5m' if gap <= 300 else '5m_to_1h' if gap <= 3600 else '1h_to_6h' if gap <= 21600 else 'over_6h'
            events.append({'r': r, 'prev': p, 'gap': gap, 'bucket': bucket,
                           'model_change': r['model'] != p['model'],
                           'after_compaction': bisect_left(bs, ts) > bisect_right(bs, pt),
                           'rewrite_ratio': ratio(r['usage']['cache_creation_input_tokens'], context(r))})
    return events, dict(excluded)


def event_stats(events, prices):
    rows = [e['r'] for e in events]
    summary = aggregate(rows, prices)
    eligible = [e for e in events if e['rewrite_ratio'] is not None]
    rewrites = sum(e['rewrite_ratio'] >= FULL_REWRITE for e in eligible)
    return {'responses': len(rows), 'write_responses': sum(r['usage']['cache_creation_input_tokens'] > 0 for r in rows),
            'write_tokens': summary['tokens']['cache_creation_input_tokens'],
            'write_p50': percentile([r['usage']['cache_creation_input_tokens'] for r in rows], .5),
            'context_p50': percentile([context(r) for r in rows], .5),
            'full_rewrites': rewrites, 'rewrite_denominator': len(eligible),
            'full_rewrite_rate': ratio(rewrites, len(eligible)),
            'write_cost_usd_range': summary['cost_components_usd_range']['cache_write'] if summary['cost_components_usd_range'] else None,
            'unpriced_responses': summary['unpriced_requests'],
            'ttl_1h_write_tokens': sum(r['cache_1h'] for r in rows),
            'ttl_5m_write_tokens': sum(r['cache_5m'] for r in rows),
            'examples': unique_evidence([e for pair in sorted(events, key=lambda e: -e['r']['usage']['cache_creation_input_tokens'])[:3]
                                         for e in (pair['prev']['evidence'], pair['r']['evidence'])])}


def matched_resume(events, prices, ttl):
    """Match execution/model and similar context; this still cannot prove expiry."""
    strata = defaultdict(lambda: {'warm': [], 'cold': []})
    for e in events:
        if e['r'].get('provider','claude') != 'claude':
            continue
        if e['model_change'] or e['after_compaction']:
            continue
        side = 'warm' if e['gap'] <= 300 else 'cold' if (
            e['gap'] > 3600 if ttl == '1h' else 300 < e['gap'] <= 3600) else None
        if side:
            strata[(e['r']['session'], e['r']['model'])][side].append(e)
    warm, cold, rejected, deltas = [], [], 0, []
    for sides in strata.values():
        w, c = sides['warm'], sides['cold']
        if not w or not c:
            rejected += len(c)
            continue
        sizes = [percentile([context(e['r']) for e in rs], .5) for rs in (w, c)]
        if not sizes[0] or not .5 <= sizes[1] / sizes[0] <= 2:
            rejected += len(c)
            continue
        warm.extend(w)
        cold.extend(c)
        rates = [ratio(sum((e['rewrite_ratio'] or 0) >= FULL_REWRITE for e in side),
                       sum(e['rewrite_ratio'] is not None for e in side)) for side in (w, c)]
        if all(r is not None for r in rates):
            deltas.append(rates[1]-rates[0])
    ws, cs = event_stats(warm, prices), event_stats(cold, prices)
    ttl_fraction = ratio(cs['ttl_' + ttl + '_write_tokens'], cs['write_tokens'])
    enough = len(warm) >= MIN_COMPARISON and len(cold) >= MIN_COMPARISON
    within_delta = ratio(sum(deltas), len(deltas))
    contrast = (enough and within_delta is not None and within_delta >= .5 and
                ws['full_rewrite_rate'] is not None and cs['full_rewrite_rate'] is not None and
                cs['full_rewrite_rate'] - ws['full_rewrite_rate'] >= .5)
    supported = bool(contrast and ttl_fraction is not None and ttl_fraction >= .9)
    return {'ttl': ttl, 'status': 'association' if supported else 'insufficient_data' if not enough else 'not_supported',
            'warm': ws, 'cold': cs, 'unmatched_cold_responses': rejected,
            'matched_strata': len(deltas), 'mean_within_stratum_rate_difference': within_delta,
            'reason': 'supported' if supported else 'insufficient_pairs' if not enough else 'ttl_unconfirmed' if contrast else 'no_rewrite_contrast',
            'observed_write_ttl_share': ttl_fraction,
            'basis': '同じ実行・モデル、モデル変更/圧縮なし、文脈中央値が0.5〜2倍の層を比較。各側3応答以上、全体と層内平均の両方で全文再書込率の差50ポイント以上、対象TTL書込90%以上。因果検定ではない。'}, cold


def component_impact(rows, prices, component):
    s = aggregate(rows, prices)
    amounts = s['cost_components_usd_range']
    field = {'cache_write': 'cache_creation_input_tokens', 'cache_read': 'cache_read_input_tokens', 'output': 'output_tokens'}.get(component)
    return {'component': component, 'responses': s['requests'], 'unpriced_responses': s['unpriced_requests'],
            'tokens': s['tokens'][field] if field else sum(s['tokens'].values()),
            'cost_usd_range': amounts[component] if amounts and component != 'total' else s['cost_usd_range']}


def analyze(report, deep, prices, by_session, compactions, ambiguous_ids, writes):
    events, excluded = transitions(by_session, compactions, ambiguous_ids)
    gap_rows = [{'code': code, 'label': label, **event_stats([e for e in events if e['bucket'] == code], prices)} for code, label in GAPS]
    signals = []
    predicates = {'model_change': lambda e: e['model_change'], 'after_compaction': lambda e: e['after_compaction'],
                  'gap_over_1h': lambda e: e['gap'] > 3600, 'gap_over_5m': lambda e: 300 < e['gap'] <= 3600}
    for code, predicate in predicates.items():
        exclusive = next(b for b in writes['buckets'] if b['code'] == code)
        signals.append({'code': code, 'inclusive': event_stats([e for e in events if predicate(e)], prices),
                        'exclusive_write_responses': exclusive['requests'], 'exclusive_write_tokens': exclusive['write_tokens']})
    findings, detectors, comparisons = [], [], []
    priced_all = bool(report['requests']) and not report['cost']['unpriced_requests']

    def add(code, title, rows, component, observation, hypothesis, alternatives, action, evidence, confidence='low', scope=None, effort='小'):
        findings.append({'id': 'finding-' + str(len(findings)+1), 'code': code, 'title': title,
                         'scope': scope, 'observation': observation, 'hypothesis': hypothesis,
                         'association_confidence': confidence, 'causal_status': 'unconfirmed',
                         'alternatives': alternatives, 'action': action, 'effort': effort,
                         'validation': '同種の代表作業で変更を1つだけ試す。根拠の正確さ・完成条件を固定し、要約/引継ぎを含む総費用、修正回数、所要時間を比較する。',
                         'impact': component_impact(rows, prices, component),
                         'evidence': unique_evidence(evidence)[:6]})

    for ttl in ('1h', '5m'):
        comparison, cold = matched_resume(events, prices, ttl)
        if not any(r.get('provider','claude') == 'claude' for r in report['requests']):
            comparison.update(status='not_applicable',reason='provider_not_applicable')
        comparisons.append(comparison)
        detectors.append({'code': 'resume_' + ttl, 'status': comparison['status']})
        if comparison['status'] == 'association':
            w, c = comparison['warm'], comparison['cold']
            add('resume_' + ttl, ('1時間超' if ttl == '1h' else '5分超〜1時間') + 'の再開時に全文再書き込みが集中',
                [e['r'] for e in cold], 'cache_write',
                f"対応する通常応答 {w['responses']}件では全文再書込 {w['full_rewrite_rate']:.1%}、再開 {c['responses']}件では {c['full_rewrite_rate']:.1%}。書込中央値 {w['write_p50']:,} → {c['write_p50']:,} tokens。",
                'キャッシュ有効期間を越えた再開による再書込と整合する。失効そのものの記録はなく、確定原因ではない。',
                ['入力の前半やツール定義の変更', '並列処理や記録時刻と実リクエスト開始時刻の差', '同じ実行内での作業内容の変化'],
                '対象の長い作業では、休憩前に決定事項・根拠の場所・未完了事項を短い引継メモに保存する。再開時は新しい会話にそのメモと必要な資料だけを渡す方法を試す。休憩後の全文圧縮にも全文処理が必要になり得るため、その費用も含める。',
                c['examples'], confidence='medium')

    polling = [r for r in report['requests'] if r.get('provider') == 'codex' and r.get('polling') and r['id'] not in ambiguous_ids]
    detectors.append({'code':'polling','status':'detected' if len(polling)>=3 else 'not_detected','responses':len(polling)})
    if len(polling)>=3:
        examples = sorted(polling,key=context,reverse=True)[:2]
        add('polling','待機・進捗確認の反復',polling,'total',
            f"待機系だけの呼出を伴うCodex応答が{len(polling)}件。入力中央値{percentile([context(r) for r in polling],.5):,} tokens。",
            '長い文脈で監視を繰り返している可能性。必要な待機も含むため無駄や削減額を確定しない。',
            ['完了・失敗・ユーザー入力を速やかに確認するために必要','前の結果を判断する推論も同じ応答に含まれる'],
            '待機・監視をシェル等の軽い処理へ寄せ、完了・失敗・進捗変化時にモデルへ戻す方式を比較する。応答性と完了条件を保つ。',
            [e for r in examples for e in r.get('polling_evidence',[])+[r['evidence']]],confidence='medium')
        findings[-1]['impact']['measurement']='responses_issuing_poll_only_calls_not_incremental_cost'

    # Persistent context is a concrete treatment candidate, never a finding of waste.
    context_count = 0
    for group in deep['groups']:
        rows = [r for sid in group['executions'] for r in by_session.get(sid, [])]
        large = [r for r in rows if r['complete'] and r['usage']['cache_read_input_tokens'] >= 50000]
        if len(large) >= 3 and len(large) >= len(rows) / 2:
            context_count += 1
            if context_count <= 10:
                add('persistent_context', '大きな文脈を繰り返し読む作業', large, 'cache_read',
                    f"作業内 {len(rows)}応答中 {len(large)}応答で5万tokens以上をキャッシュから読出。対象の入力中央値 {percentile([context(r) for r in large], .5):,} tokens。",
                    '完了した作業の背景を持ち越している可能性。大きい文脈が必要な作業という説明も残る。',
                    ['長文資料の照合に全文が必要', '複数資料の整合性維持に必要な背景'],
                    'この作業の区切りで、決定事項と出典の場所を引継メモにまとめる。次の成果物は新しい会話で必要な章・表だけを読み、全文を保持する方式と完成品質を比較する。',
                    samples(large, field='cache_read_input_tokens'), scope=group['id'])
    detectors.append({'code': 'persistent_context', 'status': 'detected' if context_count else 'not_detected', 'groups': context_count, 'shown': min(10, context_count)})

    output_groups = 0
    for group in deep['groups'][:10]:
        rows = [r for sid in group['executions'] for r in by_session.get(sid, [])]
        components = group['cost_components_usd_range']
        output_share = (ratio(components['output'][0], group['cost_usd_range'][1])
                        if components and not group['unpriced_requests'] else None)
        large_output = [r for r in rows if r['complete'] and r['usage']['output_tokens'] >= 8000]
        if len(large_output) >= 3 and (output_share is None or output_share >= .4):
            output_groups += 1
            add('large_output', '長い出力を繰り返している作業', large_output, 'output',
                f"出力8,000tokens以上の応答が {len(large_output)}件。出力には推論を含み、資料本文の長さとは限らない。",
                '全文生成の繰り返しや、必要以上に長い回答が出力を増やしている可能性。',
                ['必要な推論や長文成果物', '異なる成果物をそれぞれ生成している'],
                '対象作業の構成・合格基準・文字量を生成前に決める。修正時は変更した章や箇所だけを生成する方式を比較し、推論量と成果物本文を混同しない。',
                samples(large_output, field='output_tokens'), scope=group['id'], effort='中')
    detectors.append({'code': 'large_output', 'status': 'detected' if output_groups else 'not_detected', 'groups_in_top10': output_groups})

    repeated = next((f for f in report['findings'] if f['code'] == 'repeated_reads'), None)
    detectors.append({'code': 'repeated_reads', 'status': 'detected' if repeated else 'not_detected', 'events': report['metrics']['repeated_reads']})
    if repeated:
        add('repeated_reads', '同じ資料の同じ範囲を再取得', [], 'total', repeated['observation'],
            '抽出済みの情報や根拠を再利用できる可能性。', ['Bashや外部編集による資料更新', '正確性のための必要な再確認'],
            '対象資料から使う事実・表・出典を一度整理し、そのメモを参照する方式を比較する。更新確認は残し、毎回の全文再取得を減らせるか確認する。',
            repeated['evidence'])
        findings[-1]['impact']['measurement'] = 'not_attributed'

    # A model-change counter includes zero-write transitions; the exclusive cost does not.
    changed = [e for e in events if e['model_change']]
    isolated = [e for e in changed if not e['after_compaction'] and e['gap'] <= 300 and (e['rewrite_ratio'] or 0) >= FULL_REWRITE]
    detectors.append({'code': 'model_change', 'status': 'detected' if isolated else 'not_detected', 'all_transitions': len(changed), 'isolated_full_rewrites': len(isolated)})
    if isolated:
        add('model_change', '短い間隔でのモデル変更直後に全文再書き込み', [e['r'] for e in isolated], 'cache_write',
            f"モデル変更 {len(changed)}回のうち、圧縮なし・5分以内で全文再書込した応答は {len(isolated)}件。",
            'モデル変更に伴うキャッシュ再構築の可能性。変更しなかった場合との差額は未測定。', ['入力内容や設定の同時変更'],
            '該当する一連の作業はモデルを固定し、別モデルでの検証は必要な根拠だけを渡した別会話に分けて比較する。難しい作業の品質低下がないか確認する。',
            [x for e in isolated[:3] for x in (e['prev']['evidence'], e['r']['evidence'])])

    children = {e['id'] for e in deep['executions'] if e['subagent']}
    child_groups = 0
    for group in deep['groups'][:10]:
        rows = [r for sid in group['executions'] if sid in children for r in by_session.get(sid, [])]
        if len({r['session'] for r in rows}) < 3:
            continue
        child_groups += 1
        models = Counter(r['model'] for r in rows)
        add('subagent_fanout', '複数の調査役へ展開している作業', rows, 'total',
            f"子エージェント {len({r['session'] for r in rows})}実行、{len(rows)}応答。観測モデル: " + ', '.join(f'{m} ({n}応答)' for m, n in sorted(models.items())),
            '調査対象や渡す背景が重複している、または一部の調査が軽いモデルで足りる可能性。',
            ['独立した調査による必要な網羅性', '難しい資料の読解に現在のモデルが必要'],
            'この作業の調査役ごとの対象資料・問い・成果物を一覧化して重複を除く。単純な抽出役だけ軽いモデルで比較し、統合・根拠検証役は別に評価する。各役へ渡す背景も必要部分に絞る。',
            samples(rows), scope=group['id'], effort='中')
    detectors.append({'code': 'subagent_fanout', 'status': 'detected' if child_groups else 'not_detected', 'groups_in_top10': child_groups})

    requests = {r['id']: r for r in report['requests']}
    payloads = [e for e in deep['largest_tool_results'] if e['text_bytes'] >= 100000 and (e['neighbor_context_delta'] or 0) >= 20000]
    detectors.append({'code': 'tool_payload', 'status': 'detected' if payloads else 'not_detected', 'candidates_in_largest100': len(payloads)})
    if payloads:
        affected = {e['after_request']: requests[e['after_request']] for e in payloads if e['after_request'] in requests}
        add('tool_payload', '大きなツール結果の前後で文脈が増加', list(affected.values()), 'total',
            f"保存テキスト100KB以上かつ前後の文脈増加2万tokens以上の候補 {len(payloads)}件（結果サイズ上位100件内）。",
            'ツールが返した全文が文脈を増やした可能性。バイト数を入力トークンに換算した値ではない。',
            ['並列ツールや直前のユーザー入力も同じ応答に含まれる'],
            '該当ツールの出力を必要なページ・列・検索一致の周辺に限定する。全文はファイルに保存し、会話には要点と出典の場所を返す。欠落による調査のやり直しも測る。',
            [x for e in payloads[:2] for x in (e['call_evidence'], e['result_evidence'], e['after_evidence'])])
    comp = [e for e in deep['compactions'] if e['context_before'] is not None and e['context_after'] is not None]
    weak = [e for e in comp if e['context_before'] >= 50000 and e['context_after'] >= .9 * e['context_before']]
    detectors.append({'code': 'compaction', 'status': 'detected' if weak else 'not_detected' if comp else 'insufficient_data', 'comparable': len(comp), 'little_reduction': len(weak)})
    if weak:
        rows = {e['after_request']: requests[e['after_request']] for e in weak}
        add('compaction', '圧縮記録の前後で文脈があまり減っていない', list(rows.values()), 'total',
            f"比較できた圧縮 {len(comp)}件中 {len(weak)}件で、直前の文脈が5万tokens以上、直後も90%以上。",
            '引継ぎが長いか、圧縮直後に資料を再取得した可能性。', ['圧縮の時刻と実処理のずれ', '直後に新しい資料を必要とする作業'],
            '該当する圧縮後の読込を確認し、決定事項・必要な根拠の場所・残作業だけを引き継ぐ方式と比較する。圧縮回数だけを減らす目標にはしない。',
            [x for e in weak[:2] for x in (e['before_evidence'], e['evidence'], e['after_evidence'])])
    retries = deep['retries']
    detectors.append({'code': 'retry', 'status': 'detected' if retries else 'not_detected', 'events': len(retries)})
    if retries:
        # Reuse already de-duplicated union, rather than summing overlapping intervals.
        add('retry', '同じ引数での失敗後再試行', [], 'total',
            f"同一引数・5分以内の失敗後再試行 {len(retries)}件。引数を変えた修正や成果物の作り直しは対象外。",
            '同じ失敗を繰り返す前に入力・依存関係を検査できる可能性。', ['一時的な通信障害に対する必要な再試行'],
            '根拠のエラーを分類し、欠損ファイル・変換設定・実行環境を事前検査する。一時障害には回数上限付き再試行を使い、同じ入力で失敗を重ねない。',
            [x for e in retries[:3] for x in (e['error_evidence'], e['retry_evidence'])])
        s = deep['retry_observed_union']
        findings[-1]['impact'] = {'component': 'total', 'responses': s['requests'], 'unpriced_responses': s['unpriced_requests'],
                                  'tokens': sum(s['tokens'].values()), 'cost_usd_range': s['cost_usd_range']}
    rank = 'api_equivalent_usd_upper' if priced_all else 'observed_tokens'
    findings.sort(key=lambda f: (-(f['impact']['cost_usd_range'][1] if priced_all and f['impact']['cost_usd_range'] else f['impact']['tokens'] if not priced_all else 0), f['code'], f['scope'] or ''))
    for i, f in enumerate(findings, 1):
        f['priority'] = i
        f['classification'] = 'mechanism_hypothesis_not_problem_verdict'
    from .discovery import discover
    discovery = discover(report, deep, prices, by_session, ambiguous_ids)
    # Rule-independent work selection also covers typical and unexplained work.
    queue = [{'id': f['id'], 'kind': 'finding', 'code': f['code'], 'evidence': f['evidence']} for f in findings]
    queue.extend(discovery['review_queue'])
    checks = []
    def check(code, passed):
        checks.append({'code': code, 'passed': bool(passed)})
    check('write_partition_tokens', writes['reconciles_to_total'])
    check('write_partition_count', sum(b['requests'] for b in writes['buckets']) == sum(r['usage']['cache_creation_input_tokens'] > 0 for r in report['requests']))
    check('transition_population', len(events) + sum(excluded.values()) == len(report['requests']))
    check('gap_population', sum(g['responses'] for g in gap_rows) == len(events))
    for field in TOKEN_FIELDS:
        check('group_' + field, sum(g['tokens'][field] for g in deep['groups']) == report['totals'][field])
    costs = deep['summary']['cost_components_usd_range']
    for bound in (0, 1):
        check('write_cost_' + str(bound), costs is None or math.isclose(sum(b['cost_usd_range'][bound] for b in writes['buckets'] if b['cost_usd_range']), costs['cache_write'][bound], rel_tol=1e-8, abs_tol=1e-8))
        check('component_cost_' + str(bound), costs is None or math.isclose(sum(v[bound] for v in costs.values()), report['cost']['usd_range'][bound], rel_tol=1e-8, abs_tol=1e-8))
    check('finding_evidence', all(f['evidence'] for f in findings))
    for code, passed in discovery['checks'].items():
        check('discovery_'+code,passed)
    return {'version': 2, 'status': ('automatic_checks_passed' if report['requests'] else 'no_usage_data') if all(c['passed'] for c in checks) else 'integrity_failed',
            'discovery':discovery,
            'semantic_status': 'pending', 'ranking_basis': rank,
            'ranking_note': '対象の観測額（全応答換算時）または観測トークンの大きさ順。改善効果や削減額の順位ではない。候補間は重複するため対象額を合算しない。手間と因果の不確かさは別記。',
            'gap_analysis': {'buckets': gap_rows, 'excluded': excluded, 'signals': signals,
                             'definition': '全ての比較可能な応答を母集団とし、書込0も含む。初回・曖昧な応答を別記。全文再書込は書込 / (通常入力+書込+読出) >= 90%。間隔は応答の記録時刻で、失効の直接測定ではない。'},
            'resume_comparisons': comparisons, 'detectors': detectors, 'findings': findings,
            'checks': checks, 'review_queue': queue,
            'unknowns': ['実請求・固定席代・品質を保った削減額', 'ログにない端末・削除済み履歴・Webでの利用',
                         'キャッシュ失効の直接の証明・文脈の意味的な必要性', '変更した引数の再試行・成果物品質・人間の修正時間']}

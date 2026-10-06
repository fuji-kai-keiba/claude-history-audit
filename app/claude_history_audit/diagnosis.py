"""First-pass interpretation: observed conditions, not causal attribution or savings."""
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict

from .audit import TOKEN_FIELDS, parse_time
from .deep import aggregate, context

WRITE_LABELS = {
    'first_observed': '対象期間内の初回観測（セッション開始とは限らない）',
    'model_change': '直前の応答からモデル変更',
    'after_compaction': '直前の応答との間に圧縮記録',
    'gap_over_1h': '応答の記録間隔が1時間超（失効未確認）',
    'gap_over_5m': '応答の記録間隔が5分超〜1時間（失効未確認）',
    'multiple_signals': 'モデル変更・圧縮・長い間隔が重なる',
    'ambiguous': '順序・使用量・モデル・帰属が不明',
    'unclassified': '上記の条件なし（追記か再書込か未判定）',
}


def write_breakdown(report, prices, by_session, compactions, ambiguous_ids):
    compact_times = defaultdict(list)
    for event in compactions:
        compact_times[event['session']].append(parse_time(event['timestamp']))
    for times in compact_times.values():
        times.sort()
    buckets = defaultdict(list)
    samples = defaultdict(list)
    for sid, rows in by_session.items():
        times = [parse_time(r['timestamp']) for r in rows]
        counts = Counter(times)
        boundaries = compact_times[sid]
        for i, r in enumerate(rows):
            if not r['usage']['cache_creation_input_tokens']:
                continue
            prev = rows[i-1] if i else None
            ts = times[i]
            gap = (ts-times[i-1]).total_seconds() if prev else None
            signals = []
            ambiguous = (not r['complete'] or r['model'] == 'unknown' or counts[ts] > 1 or
                         r['id'] in ambiguous_ids or (prev and (not prev['complete'] or
                         prev['model'] == 'unknown' or counts[times[i-1]] > 1 or prev['id'] in ambiguous_ids)))
            if prev and any(bisect_left(boundaries, t) != bisect_right(boundaries, t) for t in (times[i-1], ts)):
                ambiguous = True
            if ambiguous:
                category = 'ambiguous'
            elif not prev:
                category = 'first_observed'
            else:
                if prev['model'] != r['model']:
                    signals.append('model_change')
                if bisect_left(boundaries, ts) > bisect_right(boundaries, times[i-1]):
                    signals.append('after_compaction')
                if gap > 3600:
                    signals.append('gap_over_1h')
                elif gap > 300:
                    signals.append('gap_over_5m')
                category = 'multiple_signals' if len(signals) > 1 else signals[0] if signals else 'unclassified'
            buckets[category].append(r)
            sample = {'session': sid, 'request': r['id'], 'timestamp': r['timestamp'],
                      'write_tokens': r['usage']['cache_creation_input_tokens'], 'signals': signals,
                      'gap_seconds': None if ambiguous else gap,
                      'context_delta': context(r)-context(prev) if prev and not ambiguous else None,
                      'evidence': r['evidence'], 'previous_evidence': prev['evidence'] if prev else None}
            # Keep evidence bounded; every request still contributes to its bucket.
            samples[category].append(sample)
            samples[category].sort(key=lambda e: (-e['write_tokens'], e['timestamp'], e['request']))
            del samples[category][5:]
    output = []
    for code, label in WRITE_LABELS.items():
        summary = aggregate(buckets[code], prices)
        output.append({'code': code, 'label': label, 'requests': summary['requests'],
            'write_tokens': summary['tokens']['cache_creation_input_tokens'],
            'cost_usd_range': summary['cost_components_usd_range']['cache_write'] if summary['cost_components_usd_range'] else None,
            'priced_requests': summary['priced_requests'], 'unpriced_requests': summary['unpriced_requests'],
            'examples': samples[code]})
    return {'basis': '書込を伴う応答の観測条件による排他的な分類。原因別費用・追加費用ではありません。',
            'buckets': output,
            'write_tokens': sum(b['write_tokens'] for b in output),
            'reconciles_to_total': sum(b['write_tokens'] for b in output) == report['totals']['cache_creation_input_tokens']}


def diagnose(report, deep, prices, by_session, compactions, ambiguous_ids):
    requests = report['requests']
    unpriced = [r for r in requests if r['cost_usd_range'] is None]
    ttl_unknown = [r for r in requests if r.get('provider','claude') == 'claude' and r['usage']['cache_creation_input_tokens'] > r['cache_5m'] + r['cache_1h']]
    coverage = {
        'unpriced_requests': len(unpriced),
        'unpriced_tokens': {field:sum(r['usage'][field] for r in unpriced) for field in TOKEN_FIELDS},
        'unpriced_models': sorted({r['model'] for r in unpriced}),
        'unknown_ttl_requests': len(ttl_unknown),
        'unknown_ttl_write_tokens': sum(r['usage']['cache_creation_input_tokens']-r['cache_5m']-r['cache_1h'] for r in ttl_unknown),
        'cost_is_priced_subset': bool(unpriced),
    }
    writes = write_breakdown(report, prices, by_session, compactions, ambiguous_ids)
    model_groups = defaultdict(list)
    child_ids = {e['id'] for e in deep['executions'] if e['subagent']}
    for r in requests:
        if r['session'] in child_ids:
            model_groups[r['model']].append(r)
    subagents = [{'model': model, **aggregate(rows, prices),
                  'executions': len({r['session'] for r in rows})}
                 for model, rows in sorted(model_groups.items())]
    priorities = []
    for group in deep['groups'][:3]:
        rows = [r for sid in group['executions'] for r in by_session.get(sid, [])]
        if not rows:
            continue
        largest = max(rows, key=context)
        reads = sum(r['usage']['cache_read_input_tokens'] for r in rows)
        priorities.append({'code':'top_group', 'title':'上位作業の原文と成果物を確認', 'group':group['id'],
            'observation': f"{len(group['executions'])}実行・{len(rows)}応答。キャッシュ読出 {reads:,} tokens、文脈最大 {context(largest):,} tokens。",
            'cost_usd_range': group['cost_usd_range'], 'unpriced_requests':group['unpriced_requests'],
            'evidence':[largest['evidence']],
            'hypothesis':'長い文脈の反復処理やエージェント間の重複が費用を押し上げた可能性。必要性は未確認。',
            'confirm':'根拠行から入力が増えた内容を確認し、完了済み作業・重複調査・必須の背景情報を区別する。',
            'experiment':'代表作業で要約と必要な根拠だけを引き継ぎ、同じ合格基準で総費用・品質・修正回数を比較する。'})
    write_candidates = sorted((b for b in writes['buckets'] if b['requests']), key=lambda b:-b['write_tokens'])[:3]
    for bucket in write_candidates:
        priorities.append({'code':'cache_write', 'title':bucket['label'],
            'observation':f"{bucket['requests']}応答、書込 {bucket['write_tokens']:,} tokens。", 'cost_usd_range':bucket['cost_usd_range'],
            'unpriced_requests':bucket['unpriced_requests'], 'evidence':[e['evidence'] for e in bucket['examples'][:2]],
            'hypothesis':'この分類は同時に観測した条件。初回投入、追記、失効、設定変更による再書込の原因分離は未完了。',
            'confirm':'書込の大きい根拠行と直前の行で、入力変更・間隔・モデル・圧縮を照合する。残額を通常の追記と断定しない。',
            'experiment':'同じ作業・モデルで入力配置と引継ぎ方法を1つずつ変え、書込だけでなく読出・出力・品質も比較する。'})
    from .insights import analyze
    analysis = analyze(report, deep, prices, by_session, compactions, ambiguous_ids, writes)
    return {'schema_version':2, 'status':'observed_candidates_semantic_review_required',
        'analysis': analysis,
        'cost_coverage':coverage, 'cache_writes':writes, 'subagent_models':subagents,
        'priorities':priorities,
        'interpretation_checks':[
            '金額は換算済み応答の標準API参考額。未換算は0円ではなく、書込TTL不明は上下幅を保持する。',
            '会話の継続日数や文脈量だけで最大原因・無駄を確定しない。必要な情報かを根拠行と成果物で確認する。',
            'サブエージェントのモデルはこの保存範囲の観測。常にOpusという仕様を推論しない。',
            '再試行区間の参考額は追加費用ではない。同一引数・5分以内という検出範囲から全手戻りの費用を推論しない。',
            'モデル変更直後の書込額は変更による増分ではない。未分類の書込を通常の追記で埋めない。',
            '単価を置き換えた金額はトークン量が同じという仮定の試算。品質・再試行が変わるため実削減額とは呼ばない。',
            '本文の意味・成果物品質はCLIでは未確認。初回のClaude分析で上位候補の根拠を確認し、観測・仮説・未確認を分けて報告する。']}

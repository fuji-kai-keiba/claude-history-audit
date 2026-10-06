"""Personal baselines and rule-independent exploration, never a waste classifier."""
from collections import Counter, defaultdict
import math
from statistics import median

from .audit import TOKEN_FIELDS, parse_time, percentile
from .deep import aggregate, context

MIN_RESPONSES = 12
MIN_WORKS = 6
MAX_CASES = 12
MAX_CONCENTRATION_WORKS = 20
MAX_REPRESENTATIVES = 12
TARGET_SHARE = .8
METRICS = {'context': ('入力全体', None), 'output': ('出力', 'output_tokens'),
           'input': ('通常入力', 'input_tokens'), 'write': ('キャッシュ書込', 'cache_creation_input_tokens')}


def value(r, metric):
    return r['usage'][METRICS[metric][1]] if METRICS[metric][1] else context(r)


def robust(values):
    center = median(values)
    mad = median([abs(x-center) for x in values])
    return {'median': center, 'mad': mad, 'threshold': center + max(2*center, 6*mad)}


def refs(rows):
    result, seen = [], set()
    for r in rows:
        e = r['evidence']
        key = e['file'], e['line']
        if key not in seen:
            result.append(e)
            seen.add(key)
    return result


def distribution(values):
    return {'count': len(values), 'p50': percentile(values, .5) if values else None,
            'p95': percentile(values, .95) if values else None,
            'max': max(values, default=None)}


def discover(report, deep, prices, by_session, ambiguous_ids):
    sessions = {s['id']: s for s in report['sessions']}
    all_rows = report['requests']
    known_prices = bool(all_rows) and not report['cost']['unpriced_requests']
    group_for = {sid: g['id'] for g in deep['groups'] for sid in g['executions']}
    group_rows = {g['id']: [r for sid in g['executions'] for r in by_session.get(sid, [])] for g in deep['groups']}
    groups = {g['id']: g for g in deep['groups']}
    exclusions = Counter()
    eligible = []
    for r in all_rows:
        reason = 'incomplete_usage' if not r['complete'] else 'unknown_model' if r['model']=='unknown' else 'ambiguous_owner' if r['id'] in ambiguous_ids else None
        if reason:
            exclusions[reason] += 1
        else:
            eligible.append(r)
    def cohort(r):
        s = sessions[r['session']]
        return r['model'], 'subagent' if s['subagent'] else 'main', s['workload_hint']

    population = defaultdict(list)
    for r in all_rows:
        population[cohort(r)].append(r)
    profile = [{'model': key[0], 'role': key[1], 'workload_hint': key[2],
                'responses': len(rows), 'executions': len({r['session'] for r in rows}),
                'complete_responses': sum(r['complete'] for r in rows),
                'context': distribution([context(r) for r in rows if r['complete']]),
                'output': distribution([r['usage']['output_tokens'] for r in rows if r['complete']])}
               for key, rows in sorted(population.items())]
    candidates, baseline_rows = [], []
    assessed = set()
    tied_boundaries = 0
    assessed_timelines = 0

    def add(kind, metric, rows, comparison_rows, stats, group_ids, basis, cohort_meta=None):
        summary = aggregate(rows, prices)
        candidates.append({'kind': kind, 'metric': metric, 'cohort': cohort_meta, 'groups': sorted(set(group_ids)),
                           'sample_groups': sorted({group_for[r['session']] for r in rows[:2]}),
                           'basis': basis, 'baseline': stats, 'observed_responses': len(rows),
                           'observed_max': max((value(r,metric) for r in rows), default=None) if metric in METRICS else None,
                           'impact': summary, 'evidence': refs(rows[:2]+comparison_rows[:2]),
                           'status': 'investigate_not_confirmed_problem'})

    # Response distributions are local to model/role/workload hint. Output also
    # uses a coarse input-size band; it is not a semantic equivalence claim.
    for metric in METRICS:
        cohorts = defaultdict(list)
        for r in eligible:
            band = max(1, context(r)).bit_length()-1 if metric=='output' else None
            cohorts[cohort(r)+(band,)].append(r)
        for key, rows in sorted(cohorts.items(), key=lambda pair: str(pair[0])):
            if len(rows) < MIN_RESPONSES:
                baseline_rows.append({'metric':metric, 'model':key[0], 'role':key[1], 'workload_hint':key[2],
                                      'input_size_band':key[3], 'responses':len(rows), 'status':'insufficient_data'})
                continue
            stats = robust([value(r, metric) for r in rows])
            assessed.update(r['id'] for r in rows)
            meta = {'model':key[0], 'role':key[1], 'workload_hint':key[2], 'input_size_band':key[3]}
            unusual = sorted((r for r in rows if value(r,metric)>stats['threshold']), key=lambda r: -value(r,metric))
            baseline_rows.append({'metric':metric, **meta, 'responses':len(rows),
                                  'executions':len({r['session'] for r in rows}), 'status':'assessed',
                                  **stats, 'unusual_responses':len(unusual)})
            if unusual:
                typical = sorted(rows, key=lambda r: abs(value(r,metric)-stats['median']))[:2]
                add('response_difference', metric, unusual, typical, stats,
                    [group_for[r['session']] for r in unusual],
                    '同じ保存範囲のモデル・親/子・用途の参考分類ごとの分布。判定対象も分布に含む。出力は入力規模も区分。応答の独立性や用途の一致を保証しない。', meta)

    # A sustained change may be hidden by a broad whole-period distribution.
    timelines = defaultdict(list)
    for r in eligible:
        timelines[(r['session'],r['model'])].append(r)
    for (sid, model), rows in timelines.items():
        if len(rows) < MIN_RESPONSES:
            continue
        rows = sorted(rows,key=lambda r:parse_time(r['timestamp']))
        mid = len(rows)//2
        before, after = rows[:mid], rows[mid:]
        if parse_time(before[-1]['timestamp']) >= parse_time(after[0]['timestamp']):
            tied_boundaries += 1
            continue
        assessed_timelines += 1
        for metric in ('context','output','write'):
            stats = robust([value(r,metric) for r in before])
            after_median = median([value(r,metric) for r in after])
            if after_median > stats['threshold']:
                typical_before = sorted(before,key=lambda r:abs(value(r,metric)-stats['median']))[:2]
                typical_after = sorted(after,key=lambda r:abs(value(r,metric)-after_median))
                add('within_execution_change',metric,typical_after,typical_before,
                    {**stats,'after_median':after_median,'before_responses':len(before),'after_responses':len(after)},
                    [group_for[sid]], '同じ実行・モデルを応答数で前半/後半に分けた中央値の増加。作業内容・資料量・期間の違いは原文で確認する。',
                    {'model':model,'role':cohort(rows[0])[1],'workload_hint':cohort(rows[0])[2]})

    # Work-level comparison catches thousands of individually small calls.
    work_cohorts = defaultdict(list)
    for g in deep['groups']:
        rows = group_rows[g['id']]
        if not rows or any(not r['complete'] or r['id'] in ambiguous_ids or r['model']=='unknown' for r in rows):
            continue
        root = sessions[g['id']]
        key = (tuple(sorted({r['model'] for r in rows})), root['subagent'], root['workload_hint'])
        work_cohorts[key].append(g)
    work_baselines = []
    for key, works in work_cohorts.items():
        if len(works)<MIN_WORKS:
            work_baselines.append({'models':list(key[0]),'role':'subagent' if key[1] else 'main',
                                   'workload_hint':key[2],'works':len(works),'status':'insufficient_data'})
            continue
        def work_value(g):
            return g['cost_usd_range'][1] if known_prices else sum(g['tokens'].values())
        stats=robust([work_value(g) for g in works])
        work_baselines.append({'models':list(key[0]),'role':'subagent' if key[1] else 'main',
                               'workload_hint':key[2],'works':len(works),'status':'assessed',**stats})
        typical=min(works,key=lambda g:abs(work_value(g)-stats['median']))
        for g in works:
            if work_value(g)>stats['threshold']:
                add('work_difference','work_total',group_rows[g['id']],group_rows[typical['id']][:2],
                    {**stats,'observed_value':work_value(g),'reference_works':len(works)},[g['id']],
                    '観測モデルの組合せ・親/子・用途の参考分類が同じ作業を比較。作業の難易度や成果物量は未調整。')

    def score(summary):
        return summary['cost_usd_range'][1] if known_prices and summary['cost_usd_range'] else sum(summary['tokens'].values()) if not known_prices else 0
    candidates.sort(key=lambda c:(-score(c['impact']),c['kind'],c['metric'],c['groups']))
    shown=candidates[:MAX_CASES]
    for n,c in enumerate(shown,1):
        c['id']='discovery-'+str(n)
        c['title']={'response_difference':'本人の分布から大きく外れる応答','within_execution_change':'同じ実行の途中から増加','work_difference':'同系統の作業と比べて利用が集中'}[c['kind']]
        c['metric_label']=METRICS[c['metric']][0] if c['metric'] in METRICS else '作業全体'
        c['unit']='USD（API参考額）' if c['metric']=='work_total' and known_prices else 'tokens'
        c['action']={'output':'回答の長さ・推論・同じ成果物の作り直しを根拠で区別し、必要なら出力形式や修正範囲を絞って比較する。',
                     'context':'資料の追加・履歴の持越し・並列処理を確認し、必要なら入力範囲や引継ぎ方法を比較する。',
                     'input':'新規に渡した資料・固定の説明文・設定の変化を確認し、必要なら再利用する情報の配置を比較する。',
                     'write':'新規作業・休憩・モデルや入力の変更を確認し、その環境で当てはまる条件に対してだけ改善実験を行う。',
                     'work_total':'同種の成果物に対して応答数・調査範囲・反復修正が必要だったか確認する。不要な反復が確認できた部分の手順を変える。'}[c['metric']]
        c['alternatives']=['難易度・資料量・成果物の違いによる必要な利用','用途の参考分類が不正確','その人の通常利用全体に同じ問題がある可能性']

    # Select inspection work independently of all known mechanism rules.
    ordered=sorted((g for g in deep['groups'] if group_rows[g['id']]),key=lambda g:(-score(g),g['id']))
    total=sum(score(g) for g in ordered)
    selected={}
    running=0
    for g in ordered:
        if len(selected)>=MAX_CONCENTRATION_WORKS or (running>=TARGET_SHARE*total and len(selected)>=min(10,len(ordered))):
            break
        selected[g['id']]=['concentration']
        running+=score(g)
    for c in shown:
        # Representative source groups only: a candidate can span many works.
        for gid in c['sample_groups']:
            selected.setdefault(gid,[]).append('personal_difference')
    # Sample typical work in each observed model/role/workload segment as a control.
    representatives=0
    for _,rows in sorted(population.items(),key=lambda item:-sum(sum(r['usage'].values()) for r in item[1])):
        if representatives>=MAX_REPRESENTATIVES:
            break
        center=median([context(r) for r in rows])
        r=min(rows,key=lambda r:abs(context(r)-center))
        selected.setdefault(group_for[r['session']],[]).append('representative')
        representatives+=1
    selected_ids=set(selected)
    selected_rows=[r for gid in selected for r in group_rows[gid]]
    remaining_rows=[r for g in ordered if g['id'] not in selected_ids for r in group_rows[g['id']]]
    all_summary=aggregate(all_rows,prices)
    selected_summary=aggregate(selected_rows,prices)
    remaining_summary=aggregate(remaining_rows,prices)
    queue=[{'id':c['id'],'kind':'discovery','groups':c['groups'],'evidence':c['evidence']} for c in shown]
    for gid,reasons in selected.items():
        rows=group_rows[gid]
        largest=max(rows,key=lambda r:sum(r['usage'].values()))
        queue.append({'id':'review-'+gid,'kind':'work_review','group':gid,'reasons':sorted(set(reasons)),
                      'evidence':refs([largest,rows[0],rows[-1]])})
    for q in queue:
        q['discovery_questions']=['この作業の目的と必要な成果物は何か',
            '既存の検出ルールにない手戻り・重複・不要な処理はあるか',
            '必要な処理だという反証と、まだ判断できない点は何か']
    covered_ids={r['id'] for r in selected_rows}
    checks={'selection_partition':len(covered_ids)+len(remaining_rows)==len(all_rows),
            'token_partition':all(selected_summary['tokens'][f]+remaining_summary['tokens'][f]==report['totals'][f] for f in TOKEN_FIELDS),
            'candidate_evidence':all(c['evidence'] for c in shown)}
    checks['cost_partition']=all(math.isclose(
        (selected_summary['cost_usd_range'] or [0,0])[i]+(remaining_summary['cost_usd_range'] or [0,0])[i],
        (all_summary['cost_usd_range'] or [0,0])[i],rel_tol=1e-8,abs_tol=1e-8) for i in (0,1))
    assessed_works=sum(b['works'] for b in work_baselines if b['status']=='assessed')
    return {'version':1,'owner_basis':'provided_history_scope_unverified',
            'profile':profile,'response_baselines':baseline_rows,'work_baselines':work_baselines,
            'excluded_from_response_baselines':dict(exclusions),'assessed_response_ids_count':len(assessed),
            'tied_change_boundaries':tied_boundaries,
            'assessed_works':assessed_works,'assessed_timelines':assessed_timelines,
            'eligible_responses':len(eligible),'candidates':shown,'candidate_count':len(candidates),
            'omitted_candidates':max(0,len(candidates)-len(shown)),
            'status':'differences_to_investigate' if candidates else 'no_difference_detected' if assessed or assessed_works or assessed_timelines else 'insufficient_comparison_data',
            'selection':{'basis':'api_equivalent_usd_upper' if known_prices else 'observed_tokens',
                         'target_share':TARGET_SHARE,'achieved_share':sum(score(groups[g]) for g in selected)/total if total else None,
                         'total_works':len(ordered),'selected_works':len(selected),'remaining_works':len(ordered)-len(selected),
                         'selected':selected_summary,'remaining':remaining_summary,'all':all_summary,
                         'meaning':'選んだ作業の根拠の一部を確認する範囲。原因を説明できた費用や全応答の原文確認率ではない。未選択は問題なしを意味しない。'},
            'review_queue':queue,'checks':checks,
            'parameters':{'minimum_responses':MIN_RESPONSES,'minimum_works':MIN_WORKS,
                          'threshold':'median + max(2 * median, 6 * MAD); strictly greater',
                          'max_candidates':MAX_CASES,'max_concentration_works':MAX_CONCENTRATION_WORKS,'max_representatives':MAX_REPRESENTATIVES},
            'limitations':['基準は今回取得できた本人の履歴。別の利用者の正常値や職種を仮定しない。',
                          '履歴の所有者は認証・自動分離しない。複数人分が混ざる保存先は利用者ごとに分けて監査する。',
                          '用途はキーワードの参考分類。入力規模やモデルが同じでも同じ難易度とは限らない。',
                          '中央値/MADと件数の閾値は探索用の設定。統計的有意差・正常/異常・原因を保証しない。',
                          '全体に一様にある問題は本人内比較では見えないため、代表的な作業の原文も確認する。',
                          '候補なし・資料不足・確認対象外を、問題なしや監査済みと扱わない。']}

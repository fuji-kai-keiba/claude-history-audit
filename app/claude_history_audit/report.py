"""Self-contained reports; all dynamic HTML is escaped and has no remote assets."""
import html
import json
import os
import tempfile
from pathlib import Path
from .collection import collection_notice


def fmt(value):
    return f"{value:,}" if isinstance(value, (int, float)) else "—"


def cost_text(report):
    value = report["cost"]["usd_range"]
    if value is None:
        return "単価未指定 / 換算対象なし"
    return f"${value[0]:.4f}" if abs(value[1] - value[0]) < 1e-8 else f"${value[0]:.4f}–${value[1]:.4f}"


def markdown(report):
    c, m = report["coverage"], report["metrics"]
    share = m["cache_read_token_share"]
    lines = ["# Claude Code / Codex 履歴監査", "", "集計済みの観測結果です。請求額・削減率・資料品質の確定ではありません。", "",
        "**取得範囲: " + collection_notice(report.get("collection", {"mode": "paths"})) + "**", "",
        f"- 作成日時（UTC）: {report['generated_at']}",
        "- 対象形式: " + ", ".join(p for p in ("claude", "codex") if p in report.get("providers", ["claude"])),
        f"- 保存範囲: {c['first_record']} ～ {c['last_record']}",
        f"- 指定範囲UTC（開始以上・終了未満）: {report['window']['since']} ～ {report['window']['until_exclusive']}",
        f"- ファイル: {c['files_found']} / セッション: {c['sessions']} / 一意の応答: {c['unique_requests']}",
        f"- 使用量4項目が揃う応答: {c['complete_usage_requests']} / {c['unique_requests']}",
        f"- 重複使用量レコード: {c.get('duplicate_usage_records', 0)}", "",
        "## トークンと処理", "", "| 指標 | 値 |", "|---|---:|"]
    for title, key in [("通常入力", "input_tokens"), ("キャッシュ書き込み", "cache_creation_input_tokens"),
                       ("キャッシュ読み出し", "cache_read_input_tokens"), ("出力（推論を含む）", "output_tokens")]:
        lines.append(f"| {title} | {fmt(report['totals'][key])} |")
    share_text = f"{share:.1%}" if share is not None else "—"
    lines += [f"| 入力中のキャッシュ読み出し比率 | {share_text} |",
        f"| コンテキスト P50 / P95 | {fmt(m['context_p50'])} / {fmt(m['context_p95'])} |",
        f"| ツール呼び出し / 失敗 | {m['tool_calls']} / {m['tool_errors']} |", "",
        "## API単価による参考額", "", cost_text(report), "", report["cost"]["basis"],
        "単価表の基準日: " + str(report["cost"].get("price_book_as_of") or "未指定") + "。過去の請求単価の再現ではありません。",
        f"換算済み {report['cost']['priced_requests']} / 未換算 {report['cost']['unpriced_requests']} 応答。未換算分は0円ではありません。", "",
        ]
    diagnosis = report.get("deep", {}).get("diagnosis")
    if diagnosis:
        lines += diagnosis_markdown(diagnosis)
    lines += ["## 改善候補", ""]
    for f in report["findings"]:
        lines.extend(["### " + f["title"], "", "観測: " + f["observation"], "", "解釈: " + f["interpretation"],
                      "", "次の検証: " + f["action"], ""])
        if f["evidence"]:
            lines += ["根拠: " + ", ".join(f"{e['file']}:L{e['line']}" for e in f["evidence"]), ""]
    if not report["findings"]:
        lines += ["設定した検出条件に該当しません。無駄がないことを保証するものではありません。", ""]
    if report.get("deep"):
        lines += deep_markdown(report["deep"])
    lines += ["## 取得先の状態", "", "```json", json.dumps(report.get("collection", {"mode": "paths"}), ensure_ascii=False, indent=2), "```", "",
              "## 集計のカバレッジ", "", "```json", json.dumps(c, ensure_ascii=False, indent=2), "```", "",
              "## 判断の限界", ""] + ["- " + line for line in report["limitations"]]
    lines += ["", "根拠の実ファイルは同じ出力フォルダの local-map.json で解決できます。マップは私的情報を含むため共有対象から除外してください。", ""]
    return "\n".join(lines)


def render_html(report):
    esc = lambda v: html.escape(str(v), quote=True)
    c, m, totals = report["coverage"], report["metrics"], report["totals"]
    share = m["cache_read_token_share"]
    share_text = f"{share:.1%}" if share is not None else "—"
    cards = "".join(f'<div class="stat"><span>{esc(label)}</span><strong>{esc(value)}</strong><small>{esc(note)}</small></div>'
        for label, value, note in [("一意の応答", fmt(c["unique_requests"]), f"{c['sessions']}セッションを集計"),
            ("入力の再利用", share_text, "入力中のキャッシュ読み出し比率"),
            ("コンテキスト P95", fmt(m["context_p95"]), "キャッシュ済み入力を含む"),
            ("API単価の参考額", cost_text(report), "実際の請求額ではありません")])
    total = max(1, sum(totals.values()))
    bars = "".join(f'<div class="bar-row"><span>{esc(label)}</span><div class="track"><i style="width:{100*totals[key]/total:.4f}%;background:{color}"></i></div><b>{fmt(totals[key])}</b></div>'
        for label, key, color in [("通常入力", "input_tokens", "#297969"), ("キャッシュ書込", "cache_creation_input_tokens", "#c18538"),
            ("キャッシュ読出", "cache_read_input_tokens", "#88b5a9"), ("出力", "output_tokens", "#54768f")])
    findings = "".join('<article class="finding"><div class="finding-head"><span class="tag">' + ("確認候補" if f["severity"] == "check" else "参考")
        + '</span><h3>' + esc(f["title"]) + '</h3></div><p><b>観測</b> ' + esc(f["observation"])
        + '</p><p><b>解釈</b> ' + esc(f["interpretation"]) + '</p><p><b>次の検証</b> ' + esc(f["action"])
        + '</p><details><summary>根拠の参照</summary><code>' + esc(", ".join(f"{e['file']}:L{e['line']}" for e in f["evidence"]) or "集計結果")
        + '</code></details></article>' for f in report["findings"])
    if not findings:
        findings = '<p class="muted">検出条件に該当する候補はありません。無駄がないことを保証するものではありません。</p>'
    model_rows = "".join(f'<tr><td>{esc(r["model"])}</td><td>{fmt(r["requests"])}</td><td>{fmt(sum(r["tokens"].values()))}</td><td>{fmt(r["tokens"]["output_tokens"])}</td></tr>' for r in report["models"])
    session_rows = "".join(f'<tr><td><code>{esc(s["id"])}</code><small>{esc(s["first"])}</small></td><td>{esc(s["workload_hint"])}</td><td>{s["requests"]}</td><td>{fmt(s["tokens"])}</td><td>{s["repeated_reads"]}</td><td>{s["tool_errors"]}</td></tr>' for s in report["sessions"][:200])
    coverage_rows = "".join(f'<tr><td>{esc(k)}</td><td>{esc(v)}</td></tr>' for k, v in c.items())
    limitations = "".join('<li>' + esc(line) + '</li>' for line in report["limitations"])
    collection = report.get("collection", {"mode": "paths"})
    scope = '<section class="panel"><h2>今回の取得範囲</h2><div class="notice">' + esc(collection_notice(collection)) + '</div>'
    if collection.get("sources"):
        scope += '<div class="scroll"><table><tr><th>取得先ID</th><th>方法</th><th>状態</th><th>ファイル数</th><th>除外・エラー</th></tr>'
        labels = {"ok": "取得済み", "empty": "履歴なし", "partial": "一部除外", "failed": "取得失敗"}
        for source in collection["sources"]:
            scope += '<tr>' + ''.join('<td>' + esc(v) + '</td>' for v in [source["id"], source["kind"], labels[source["status"]], source["files"], source["error"] or "—"]) + '</tr>'
        scope += '</table></div>'
    scope += '<p class="meta">指定範囲UTC（開始以上・終了未満）: ' + esc(report['window']['since']) + ' → ' + esc(report['window']['until_exclusive']) + '</p>'
    scope += '<p class="meta">単価表の基準日: ' + esc(report['cost'].get('price_book_as_of') or '未指定') + '。過去の請求単価の再現ではありません。</p></section>'
    return '''<!doctype html>
<html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'">
<title>Claude Code / Codex 履歴監査</title><style>
:root{color-scheme:light;--ink:#172d31;--muted:#597073;--line:#d9e2de;--paper:#f4f6f2;--accent:#297969}*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:15px/1.8 -apple-system,BlinkMacSystemFont,"Hiragino Kaku Gothic ProN",Meiryo,sans-serif}main{max-width:1200px;margin:auto;padding:46px 32px 70px}header{border-bottom:1px solid var(--line);padding-bottom:27px}.eyebrow{letter-spacing:.16em;font-size:12px;color:var(--accent);font-weight:700}h1{font-size:34px;letter-spacing:-.04em;line-height:1.4;margin:12px 0}h2{font-size:20px;margin:0 0 17px}h3{font-size:17px;margin:0}.muted,small{color:var(--muted)}header p{margin:8px 0}.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin:26px 0}.stat{background:#fff;border:1px solid var(--line);border-radius:12px;padding:21px}.stat span{color:var(--muted);font-size:13px}.stat strong{display:block;font-size:27px;line-height:1.5;margin:9px 0;overflow-wrap:anywhere}.stat small{font-size:11px;display:block}.grid{display:grid;grid-template-columns:1fr 1fr;gap:20px}.panel{background:#fff;border:1px solid var(--line);border-radius:12px;padding:25px;margin-bottom:20px}.bar-row{display:grid;grid-template-columns:120px 1fr 105px;gap:12px;align-items:center;margin:14px 0;font-size:13px}.bar-row b{text-align:right;font-variant-numeric:tabular-nums}.track{height:13px;border-radius:3px;background:#edf1ed;overflow:hidden}.track i{height:100%;display:block}.notice{background:#edf4f0;padding:17px;border-left:3px solid var(--accent);font-size:13px}.finding{border-top:1px solid var(--line);padding:22px 0}.finding:first-of-type{border-top:0;padding-top:0}.finding:last-child{padding-bottom:0}.finding-head{display:flex;gap:12px;align-items:center}.tag{font-size:11px;color:#775420;background:#f7eedc;border-radius:4px;padding:2px 8px;white-space:nowrap}.finding p{margin:8px 0;font-size:14px}.finding p b{margin-right:8px}details{font-size:12px;color:var(--muted)}summary{cursor:pointer}code{font-family:ui-monospace,SFMono-Regular,monospace;font-size:12px;overflow-wrap:anywhere}.scroll{overflow-x:auto}.scroll table{min-width:640px}table{width:100%;border-collapse:collapse;text-align:left;font-size:13px}th{color:var(--muted);font-weight:500;font-size:12px}td,th{padding:12px 10px;border-bottom:1px solid var(--line);vertical-align:top}td small{display:block;font-size:11px}input{font:inherit;padding:9px 13px;border:1px solid var(--line);border-radius:7px;max-width:100%;width:340px;margin-bottom:15px}ul{padding-left:21px;font-size:13px}footer{font-size:12px;color:var(--muted)}.meta{font-size:12px}.metrics{display:flex;gap:25px;flex-wrap:wrap}.metrics strong{font-size:25px;display:block}.metrics span{font-size:12px;color:var(--muted)}@media(max-width:900px){.cards{grid-template-columns:repeat(2,1fr)}.grid{grid-template-columns:1fr}}@media(max-width:550px){main{padding:24px 15px}.cards{gap:8px}.stat{padding:14px}.stat strong{font-size:22px}h1{font-size:27px}.panel{padding:18px}.bar-row{grid-template-columns:90px 1fr 80px;gap:7px;font-size:11px}}@media print{body{background:#fff}main{max-width:none;padding:0}.panel,.stat{break-inside:avoid}.cards{grid-template-columns:repeat(4,1fr)}input{display:none}}
</style></head><body><main><header><div class="eyebrow">CLAUDE HISTORY AUDIT · LOCAL REPORT</div><h1>履歴から、次の改善を見つける。</h1><p>保存済みの作業を集計した監査レポート。観測と改善仮説を分けて確認します。</p><p class="meta">''' + esc(c["first_record"]) + ' → ' + esc(c["last_record"]) + '（UTC） · 作成 ' + esc(report["generated_at"]) + '</p></header>' + scope + '<div class="cards">' + cards + '''</div><div class="grid"><section class="panel"><h2>トークンの内訳</h2>''' + bars + '''<p class="muted meta">読み出しは低単価のキャッシュを含みます。トークン数と費用は比例しません。</p></section><section class="panel"><h2>処理とカバレッジ</h2><div class="metrics">''' + ''.join(f'<div><strong>{fmt(v)}</strong><span>{esc(k)}</span></div>' for k, v in [("対象ファイル", c["files_found"]), ("ツール実行", m["tool_calls"]), ("明示的な失敗", m["tool_errors"])]) + '</div><p class="meta">使用量が揃う応答 ' + f'{c["complete_usage_requests"]} / {c["unique_requests"]}' + ' · 重複レコード ' + str(c.get("duplicate_usage_records", 0)) + '</p><div class="notice">' + esc(report["cost"]["basis"]) + f'<br>換算済み {report["cost"]["priced_requests"]} / 未換算 {report["cost"]["unpriced_requests"]}。未換算分は0円ではありません。' + '</div></section></div>' + diagnosis_html(report.get("deep", {}).get("diagnosis")) + '<section class="panel"><h2>根拠付きの改善候補</h2>' + findings + '</section>' + deep_html(report.get("deep")) + '<section class="panel"><h2>モデル別の利用</h2><div class="scroll"><table><thead><tr><th>モデル</th><th>応答</th><th>総トークン</th><th>出力</th></tr></thead><tbody>' + model_rows + '</tbody></table></div></section><section class="panel"><h2>セッション別の利用</h2><p class="meta muted">総トークン順、最大200件。作業分類はキーワードによる参考値。全件は report.json に保存しています。</p><input id="filter" aria-label="セッションを絞り込む" placeholder="分類・セッションIDで絞り込み"><div class="scroll"><table id="sessions"><thead><tr><th>セッション / 開始UTC</th><th>作業の参考分類</th><th>応答</th><th>総トークン</th><th>再読込</th><th>失敗</th></tr></thead><tbody>' + session_rows + '</tbody></table></div></section><section class="panel"><h2>この監査で判断できないこと</h2><ul>' + limitations + '</ul><details><summary>欠損・除外を含む集計の詳細</summary><div class="scroll"><table>' + coverage_rows + '</table></div></details></section><footer>このレポート画面は通信しません。会話本文とパスはこの画面に含めません。根拠の実ファイルはローカルの local-map.json で確認できます。</footer></main><script>document.getElementById("filter").addEventListener("input",function(){const q=this.value.toLowerCase();document.querySelectorAll("#sessions tbody tr").forEach(function(row){row.hidden=!row.textContent.toLowerCase().includes(q);});});</script></body></html>'


def write_reports(destination, report, local_map):
    destination = Path(destination).expanduser().absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError("出力先は新しいフォルダを指定してください。既存の監査結果は上書きしません。")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".audit-", dir=str(destination.parent)) as temp:
        staging = Path(temp) / "report"
        staging.mkdir(mode=0o700)
        outputs = {"report.json": json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                   "local-map.json": json.dumps(local_map, ensure_ascii=False, indent=2) + "\n",
                   "summary.md": markdown(report), "report.html": render_html(report)}
        from .evidence import plan_for, notes_for, snapshot_for
        plan = plan_for(report)
        if plan:
            outputs['review-plan.json'] = json.dumps(plan, ensure_ascii=False, indent=2) + '\n'
            outputs['review-notes.private.json'] = json.dumps(notes_for(plan), ensure_ascii=False, indent=2) + '\n'
            outputs['evidence-snapshot.private.json'] = json.dumps(snapshot_for(local_map, plan), ensure_ascii=False, indent=2) + '\n'
        for name, content in outputs.items():
            fd = os.open(str(staging / name), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(content)
        os.rename(staging, destination)
    return destination


def money(value):
    if value is None:
        return "未換算"
    return f"${value[0]:.4f}" if abs(value[1] - value[0]) < 1e-8 else f"${value[0]:.4f}–${value[1]:.4f}"


def evidence_text(value):
    return f"{value['file']}:L{value['line']}" if value else "—"


def deep_markdown(deep):
    coverage = deep["coverage"]
    lines = ["## 深掘り監査", "",
        f"親実行 {coverage['main_executions']} / サブエージェント {coverage['subagent_executions']} / 親に紐付け済み {coverage['linked_subagents']} / 親不明 {coverage['unlinked_subagents']}", "",
        "順位: " + ("API参考額の上限順" if deep["ranking_basis"] == "api_equivalent_usd_upper" else "入力トークン順（未換算あり）"), "",
        "### 親子を合わせた上位10作業", "", "| 作業ID | 実行数 | 応答 | 入力合計 | API参考額 | 未換算応答 |",
        "|---|---:|---:|---:|---:|---:|"]
    for g in deep["groups"][:10]:
        inputs = sum(v for k, v in g["tokens"].items() if k != "output_tokens")
        lines.append(f"| {g['id']} | {len(g['executions'])} | {g['requests']} | {fmt(inputs)} | {money(g['cost_usd_range'])} | {g['unpriced_requests']} |")
    lines += ["", "### 費用の内訳（換算できた応答のみ）", ""]
    parts = deep["summary"]["cost_components_usd_range"]
    if parts:
        lines += [f"- {label}: {money(parts[key])}" for key, label in
                  [("input", "通常入力"), ("cache_write", "キャッシュ書込"), ("cache_read", "キャッシュ読出"), ("output", "出力")]]
    else:
        lines.append("単価が不明のため未換算。")
    lines += ["", f"再試行候補 {len(deep['retries'])} / 圧縮境界 {len(deep['compactions'])} / 結果対応済み {coverage['matched_tool_results']} / 対応不明 {coverage['unmatched_tool_results']}。", "",
              "再試行区間の重複除外済み観測参考額: " + money(deep["retry_observed_union"]["cost_usd_range"]) + "。失敗が原因の費用・削減可能額ではありません。", "",
              "### 詳細確認の入口", ""]
    for e in deep["executions"][:5]:
        peak = max(e["trajectory"], key=lambda p: p["context_tokens"], default=None)
        if peak:
            lines.append(f"- {e['id']}: 文脈ピーク {fmt(e['context_peak'])}、根拠 {evidence_text(peak['evidence'])}")
    for r in deep["largest_tool_results"][:5]:
        lines.append(f"- {r['tool']}: 保存テキスト {fmt(r['text_bytes'])} bytes、結果根拠 {evidence_text(r['result_evidence'])}")
    lines += ["", "### 深掘りの解釈", ""] + ["- " + line for line in deep["interpretation"]] + [""]
    return lines


def deep_html(deep):
    if not deep:
        return '<section class="panel"><h2>詳細な原因調査</h2><p>--deep を付けて再実行すると文脈推移・親子集計・再試行・結果量・圧縮前後を確認できます。</p></section>'
    esc = lambda value: html.escape(str(value), quote=True)
    def table(headers, rows):
        return '<div class="scroll"><table><thead><tr>' + ''.join('<th>' + esc(h) + '</th>' for h in headers) + '</tr></thead><tbody>' + ''.join('<tr>' + ''.join('<td>' + esc(v) + '</td>' for v in row) + '</tr>' for row in rows) + '</tbody></table></div>'
    def section(title, body):
        return '<section class="panel"><h2>' + esc(title) + '</h2>' + body + '</section>'
    c = deep["coverage"]
    rank = "API参考額の上限順" if deep["ranking_basis"] == "api_equivalent_usd_upper" else "入力トークン順（未換算あり）"
    result = section("深掘り：親子を合わせた上位10作業", '<p class="meta">' + esc(
        f"親実行 {c['main_executions']} / サブエージェント {c['subagent_executions']} / 紐付け済み {c['linked_subagents']} / 親不明 {c['unlinked_subagents']}。{rank}。親子は保存構造からの推定。") + '</p>' +
        table(["作業ID", "実行数", "応答", "入力合計", "API参考額", "未換算"],
              [[g["id"] + ("（親不明）" if g["unlinked_subagent"] else ""), len(g["executions"]), fmt(g["requests"]),
                fmt(sum(v for k, v in g["tokens"].items() if k != "output_tokens")), money(g["cost_usd_range"]), g["unpriced_requests"]] for g in deep["groups"][:10]]))
    parts = deep["summary"]["cost_components_usd_range"]
    result += section("どの種類のトークンに費用が掛かるか", '<p class="meta">換算できた応答のみ。書込TTL不明は金額の幅で表示。実請求・削減可能額ではありません。</p>' +
        table(["種別", "API参考額"], [[label, money(parts[key]) if parts else "未換算"] for key, label in
              [("input", "通常入力"), ("cache_write", "キャッシュ書込"), ("cache_read", "キャッシュ読出"), ("output", "出力")]]))
    body = '<p class="meta">上位10実行。折りたたみ内は先頭200応答、全応答は report.json。線は入力トークンの推移で時刻間隔は等間隔表示です。</p>'
    for e in deep["executions"][:10]:
        points = e["trajectory"]
        if not points:
            continue
        sampled = points if len(points) <= 160 else [points[round(i * (len(points)-1) / 159)] for i in range(160)]
        peak = max(1, e["context_peak"])
        coords = ' '.join(f'{10 + i * 780 / max(1,len(sampled)-1):.2f},{105 - p["context_tokens"] * 95 / peak:.2f}' for i, p in enumerate(sampled))
        body += '<article class="finding"><h3><code>' + esc(e["id"]) + '</code></h3><p class="meta">' + esc(
            f"{'サブエージェント' if e['subagent'] else '親実行'} / 応答 {e['requests']} / 文脈 P50 {fmt(e['context_p50'])} / 最大 {fmt(e['context_peak'])} / {money(e['cost_usd_range'])} / 未換算 {e['unpriced_requests']}") + '</p>'
        body += '<svg viewBox="0 0 800 115" role="img" aria-label="入力トークン推移" style="width:100%;max-height:115px"><polyline points="' + coords + '" fill="none" stroke="#297969" stroke-width="2"/></svg>'
        body += '<details><summary>応答ごとの増減と根拠</summary>' + table(["記録UTC", "文脈", "前回との差", "書込", "出力", "根拠"],
            [[p["timestamp"], fmt(p["context_tokens"]), fmt(p["delta_tokens"]), fmt(p["cache_write_tokens"]), fmt(p["output_tokens"]), evidence_text(p["evidence"])] for p in points[:200]]) + '</details></article>'
    result += section("文脈が増えた箇所を追う", body)
    result += section("大きなツール結果と前後の文脈", '<p class="meta">保存されたUTF-8テキストの大きい順、上位20件。バイト数はトークン数ではありません。前後差とツール結果の因果関係は未確定です。</p>' +
        table(["ツール", "実行", "テキストbytes", "前後の文脈差", "結果の根拠", "後の応答の根拠"],
              [[r["tool"], r["session"], fmt(r["text_bytes"]), fmt(r["neighbor_context_delta"]), evidence_text(r["result_evidence"]), evidence_text(r["after_evidence"])] for r in deep["largest_tool_results"][:20]]))
    retries = sorted(deep["retries"], key=lambda r: -sum(r["observed_interval"]["tokens"].values()))
    result += section("失敗後に同じ引数で再実行した箇所", '<p class="meta">5分以内の同一引数による再呼出。区間の総トークン順、上位20件。区間には他の正常処理を含み、重なりもあるため行の金額は合計しません。</p><p>重複除外した区間の観測参考額: ' + esc(money(deep["retry_observed_union"]["cost_usd_range"])) + '（削減可能額ではありません）</p>' +
        table(["ツール", "待ち秒", "再実行結果", "区間の応答", "区間API参考額", "未換算", "失敗の根拠", "再実行の根拠"],
              [[r["tool"], r["delay_seconds"], {"success":"成功", "error":"失敗", "unobserved":"結果なし", "unknown":"不明", "conflicting":"不一致"}[r["outcome"]],
                r["observed_interval"]["requests"], money(r["observed_interval"]["cost_usd_range"]), r["observed_interval"]["unpriced_requests"], evidence_text(r["error_evidence"]), evidence_text(r["retry_evidence"])] for r in retries[:20]]))
    result += section("圧縮の前後", '<p class="meta">記録時刻順、先頭20件。時刻が同じ・使用量欠損・前後の応答が対象外の場合は比較できません。</p>' +
        table(["圧縮UTC", "前の文脈", "後の文脈", "差", "直後の書込", "圧縮の根拠"],
              [[r["timestamp"], fmt(r["context_before"]), fmt(r["context_after"]), fmt(r["context_delta"]), fmt(r["next_cache_write_tokens"]), evidence_text(r["evidence"])] for r in deep["compactions"][:20]]))
    result += section("深掘りのカバレッジと解釈", '<ul>' + ''.join('<li>' + esc(line) + '</li>' for line in deep["interpretation"]) + '</ul><details><summary>対応できなかった記録と欠損</summary>' + table(["項目", "件数"], c.items()) + '</details>')
    return result



def diagnosis_markdown(diagnosis):
    c = diagnosis["cost_coverage"]
    lines = analysis_markdown(diagnosis.get('analysis')) + ["## 初回診断：費用の観測と未確認の原因", "",
             "CLIの原因候補です。本文の意味・成果物品質は未確認。利用中のAIで根拠行の確認まで進めてから結論にします。", "",
             f"未換算 {c['unpriced_requests']} 応答、未換算の観測トークン {fmt(sum(c['unpriced_tokens'].values()))}。",
             f"書込TTL不明 {c['unknown_ttl_requests']} 応答 / {fmt(c['unknown_ttl_write_tokens'])} tokens。金額の上下幅を単一額に置き換えない。", "",
             "### キャッシュ書込の観測条件別内訳", "", diagnosis["cache_writes"]["basis"], "",
             "| 条件 | 応答 | 書込tokens | 書込参考額 | 未換算応答 |", "|---|---:|---:|---:|---:|"]
    for b in diagnosis["cache_writes"]["buckets"]:
        lines.append(f"| {b['label']} | {b['requests']} | {fmt(b['write_tokens'])} | {money(b['cost_usd_range'])} | {b['unpriced_requests']} |")
    lines += ["", "各書込応答は1分類だけに含み、未分類を含めたトークン合計を照合済み。金額は各分類の換算できた書込だけです。", "",
              "### サブエージェントのモデル実績", "", "| モデル | 実行 | 応答 | API参考額 | 未換算 |", "|---|---:|---:|---:|---:|"]
    for item in diagnosis["subagent_models"]:
        lines.append(f"| {item['model']} | {item['executions']} | {item['requests']} | {money(item['cost_usd_range'])} | {item['unpriced_requests']} |")
    lines += ["", "モデルを途中で変えた実行は複数行に現れるため、実行数は行間で合算しない。", ""]
    if not diagnosis.get('analysis'):
        lines += ["### 初回に確認する根拠と比較方法", ""]
    for p in ([] if diagnosis.get('analysis') else diagnosis["priorities"]):
        lines += ["#### " + p["title"], "", "観測: " + p["observation"] + " 観測参考額 " + money(p["cost_usd_range"]) + f" / 未換算 {p['unpriced_requests']} 応答。",
                  "", "仮説（未確認）: " + p["hypothesis"], "", "根拠: " + ", ".join(evidence_text(e) for e in p["evidence"]),
                  "", "原文確認: " + p["confirm"], "", "比較実験: " + p["experiment"], ""]
    lines += ["### 結論を出す前の点検", ""] + ["- " + line for line in diagnosis["interpretation_checks"]] + [""]
    return lines


def diagnosis_html(diagnosis):
    if not diagnosis:
        return ""
    esc = lambda value: html.escape(str(value), quote=True)
    c = diagnosis["cost_coverage"]
    body = analysis_html(diagnosis.get('analysis')) + '<section class="panel"><h2>初回診断：費用の観測と未確認の原因</h2><div class="notice">本文の意味・成果物品質はCLIでは未確認。根拠行を確認する前に、候補を確定原因や削減額と呼ばないでください。</div><p>'
    body += esc(f"未換算 {c['unpriced_requests']} 応答 / {fmt(sum(c['unpriced_tokens'].values()))} tokens。書込TTL不明 {c['unknown_ttl_requests']} 応答 / {fmt(c['unknown_ttl_write_tokens'])} tokens。") + '</p>'
    body += '<h3>キャッシュ書込の観測条件別内訳</h3><p class="meta">' + esc(diagnosis["cache_writes"]["basis"]) + '</p><div class="scroll"><table><tr><th>条件</th><th>応答</th><th>書込tokens</th><th>書込参考額</th><th>未換算</th></tr>'
    for b in diagnosis["cache_writes"]["buckets"]:
        body += '<tr>' + ''.join('<td>' + esc(v) + '</td>' for v in [b['label'],b['requests'],fmt(b['write_tokens']),money(b['cost_usd_range']),b['unpriced_requests']]) + '</tr>'
    body += '</table></div><p class="meta">各書込応答は1分類にだけ計上。金額は換算できた書込の小計です。条件の重なり・不明・未分類を別に残します。</p>'
    body += '<h3>サブエージェントのモデル実績</h3><div class="scroll"><table><tr><th>モデル</th><th>実行</th><th>応答</th><th>参考額</th><th>未換算</th></tr>'
    for item in diagnosis["subagent_models"]:
        body += '<tr>' + ''.join('<td>' + esc(v) + '</td>' for v in [item['model'],item['executions'],item['requests'],money(item['cost_usd_range']),item['unpriced_requests']]) + '</tr>'
    body += '</table></div><p class="meta">モデル変更した実行は複数行に現れます。実行数を行間で合算しないでください。</p>'
    if not diagnosis.get('analysis'):
        body += '<h3>初回に確認する根拠と比較方法</h3>'
    for p in ([] if diagnosis.get('analysis') else diagnosis['priorities']):
        body += '<article class="finding"><h3>' + esc(p['title']) + '</h3><p><b>観測</b> ' + esc(p['observation']) + ' / ' + esc(money(p['cost_usd_range'])) + esc(f" / 未換算 {p['unpriced_requests']} 応答") + '</p>'
        for label, key in [('仮説（未確認）','hypothesis'),('原文確認','confirm'),('比較実験','experiment')]:
            body += '<p><b>' + label + '</b> ' + esc(p[key]) + '</p>'
        body += '<p class="meta">根拠: ' + esc(', '.join(evidence_text(e) for e in p['evidence'])) + '</p></article>'
    return body + '<h3>結論を出す前の点検</h3><ul>' + ''.join('<li>' + esc(t) + '</li>' for t in diagnosis['interpretation_checks']) + '</ul></section>'


def analysis_markdown(analysis):
    if not analysis:
        return []
    a = analysis
    lines = discovery_markdown(a.get('discovery')) + ['## 原因を調べるための手がかり', '',
             '整合性: ' + a['status'] + '。原文の意味確認: 未完了（review-plan.json）。', '',
             '以下の固定ルールは処理の特徴を示す候補で、本人にとって異常・不要だという判定ではありません。', '',
             a['ranking_note'], '', '### 休憩時間と書き込みの比較', '', a['gap_analysis']['definition'], '',
             '| 直前の応答からの間隔 | 応答 | 書込あり | 書込中央値 | 全文再書込 / 分母 | 比率 | 書込参考額 | 未換算 |',
             '|---|---:|---:|---:|---:|---:|---:|---:|']
    for b in a['gap_analysis']['buckets']:
        rate = f"{b['full_rewrite_rate']:.1%}" if b['full_rewrite_rate'] is not None else '—'
        lines.append(f"| {b['label']} | {b['responses']} | {b['write_responses']} | {fmt(b['write_p50'])} | {b['full_rewrites']} / {b['rewrite_denominator']} | {rate} | {money(b['write_cost_usd_range'])} | {b['unpriced_responses']} |")
    lines += ['', '比較から除外: ' + json.dumps(a['gap_analysis']['excluded'], ensure_ascii=False), '',
              '### 重なる条件と排他的な分類を区別', '',
              '| 条件 | 条件を満たす全応答 | うち書込あり | 他の条件と重ならない書込応答 |', '|---|---:|---:|---:|']
    for s in a['gap_analysis']['signals']:
        lines.append(f"| {s['code']} | {s['inclusive']['responses']} | {s['inclusive']['write_responses']} | {s['exclusive_write_responses']} |")
    lines += ['', '条件別の全応答は重複します。排他的な書込内訳と母集団が異なるため、件数を混同しない。', '', '### 同じ実行・モデルでの休憩比較', '']
    for c in a['resume_comparisons']:
        reasons = {'supported':'関連を支持する観測あり', 'insufficient_pairs':'比較対象が不足', 'ttl_unconfirmed':'TTLの裏付けが不足', 'no_rewrite_contrast':'層内と全体で一貫した差を検出せず','provider_not_applicable':'対象外（CodexにClaudeのTTLを適用しない）'}
        lines += [f"- TTL {c['ttl']}: {reasons[c['reason']]}。通常 {c['warm']['responses']} / 再開 {c['cold']['responses']} 応答。比較できない再開 {c['unmatched_cold_responses']} 応答。", '  ' + c['basis']]
    lines += ['', '### 原文で必要性を確認してから選ぶ改善実験', '']
    if not a['findings']:
        lines += ['設定した条件を満たす改善候補はありません。履歴の原文確認や品質評価が不要という意味ではありません。', '']
    for f in a['findings']:
        impact = f['impact']
        lines += [f"#### {f['priority']}. {f['title']} ({f['id']})", '',
                  '対象作業: ' + (f['scope'] or '根拠に示す実行・応答'), '',
                  f"対象の観測参考額: {money(impact['cost_usd_range'])} / {impact['component']}。未換算 {impact['unpriced_responses']} 応答。削減可能額ではない。", '',
                  '観測: ' + f['observation'], '', '原因仮説（未確定）: ' + f['hypothesis'], '',
                  '代替説明: ' + ' / '.join(f['alternatives']), '',
                  f"関連の確度: {f['association_confidence']}。変更の手間: {f['effort']}。", '',
                  '**変更案:** ' + f['action'], '', '比較方法: ' + f['validation'], '',
                  '根拠: ' + ', '.join(evidence_text(e) for e in f['evidence']), '']
    lines += ['### 検査の実施状況', '']
    lines += ['- ' + d['code'] + ': ' + d['status'] for d in a['detectors']]
    lines += ['', '合計・母集団・根拠検査: ' + ', '.join(c['code'] + '=' + ('PASS' if c['passed'] else 'FAIL') for c in a['checks']), '',
              '### 未確認の範囲', ''] + ['- ' + s for s in a['unknowns']] + ['']
    return lines


def discovery_markdown(d):
    if not d:
        return []
    s=d['selection']
    status={'differences_to_investigate':'本人の分布と異なる候補あり',
            'no_difference_detected':'設定した比較では差を検出せず',
            'insufficient_comparison_data':'比較できる記録が不足'}[d['status']]
    lines=['## 利用者自身の履歴から調べる', '',status+'。問題の有無を確定した結果ではありません。', '',
           '### 観測した使い方', '',
           '| モデル | 親/子 | 用途の参考分類 | 応答 | 使用量が揃う応答 | 入力中央値 | 出力中央値 |',
           '|---|---|---|---:|---:|---:|---:|']
    for p in d['profile']:
        lines.append(f"| {p['model']} | {p['role']} | {p['workload_hint']} | {p['responses']} | {p['complete_responses']} | {fmt(p['context']['p50'])} | {fmt(p['output']['p50'])} |")
    lines += ['',f"本人内の応答分布を比較できた範囲: {d['assessed_response_ids_count']} 応答。少数・欠損・比較できない条件は無理に正常判定しません。", '',
              '### 固定の問題パターンに依存しない確認対象', '',
              f"全 {s['total_works']} 作業から {s['selected_works']} 作業を選択。未選択 {s['remaining_works']} 作業。", '',
              f"選択した作業の参考額 {money(s['selected']['cost_usd_range'])} / 未選択 {money(s['remaining']['cost_usd_range'])}。それぞれ未換算 {s['selected']['unpriced_requests']} / {s['remaining']['unpriced_requests']} 応答。", '',
              '選択基準: '+('観測参考額' if s['basis']=='api_equivalent_usd_upper' else '観測トークン（価格の欠損あり）')+
              '。選んだ作業が占める割合: '+(f"{s['achieved_share']:.1%}" if s['achieved_share'] is not None else '算出不可')+'。', '',
              s['meaning'], '', '利用が集中する作業に加え、本人内の差がある作業と通常の代表例を確認します。普段から一様にある問題は比較だけでは見つからないため、用途と必要性も原文で調べます。', '',
              '### 本人内で差がある箇所', '',
              f"候補 {d['candidate_count']} 件 / 表示・確認対象 {len(d['candidates'])} 件 / 対象外 {d['omitted_candidates']} 件。対象額は重なるため合算しません。", '']
    for c in d['candidates']:
        b=c['baseline']
        lines += [f"#### {c['title']}：{c['metric_label']} ({c['id']})", '',
                  f"基準の中央値 {fmt(b['median'])}、ばらつきMAD {fmt(b['mad'])}、探索用の境界 {fmt(b['threshold'])}（{c['unit']}）。対象 {c['observed_responses']} 応答。", '',
                  '観測値: '+fmt(b.get('observed_value',b.get('after_median',c['observed_max'])))+'。対象応答の参考額 '+money(c['impact']['cost_usd_range'])+'。', '',
                  c['basis'], '', '考えられる別の説明: '+' / '.join(c['alternatives']), '',
                  '確認と変更案: '+c['action'], '',
                  '根拠（大きい側と通常側）: '+', '.join(evidence_text(e) for e in c['evidence']), '']
    lines += ['### この比較で判断できないこと', '']+['- '+s for s in d['limitations']]+['']
    return lines


def analysis_html(analysis):
    """Render our generated Markdown subset without external dependencies or raw HTML."""
    lines = analysis_markdown(analysis)
    if not lines:
        return ''
    body = '<section class="panel">'
    in_table = False
    for line in lines:
        if line.startswith('|'):
            if set(line.replace('|', '').replace('-', '').replace(':', '').strip()) == set():
                continue
            if not in_table:
                body += '<div class="scroll"><table>'
                in_table = True
            body += '<tr>' + ''.join('<td>' + html.escape(v.strip()) + '</td>' for v in line.strip('|').split('|')) + '</tr>'
            continue
        if in_table:
            body += '</table></div>'
            in_table = False
        if line.startswith('#### '):
            body += '<h4>' + html.escape(line[5:]) + '</h4>'
        elif line.startswith('### '):
            body += '<h3>' + html.escape(line[4:]) + '</h3>'
        elif line.startswith('## '):
            body += '<h2>' + html.escape(line[3:]) + '</h2>'
        elif line:
            body += '<p>' + html.escape(line.replace('**', '')) + '</p>'
    return body + ('</table></div>' if in_table else '') + '</section>'

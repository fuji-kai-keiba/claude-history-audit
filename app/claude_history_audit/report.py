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
    lines = ["# Claude Code 履歴監査", "", "集計済みの観測結果です。請求額・削減率・資料品質の確定ではありません。", "",
        "**取得範囲: " + collection_notice(report.get("collection", {"mode": "paths"})) + "**", "",
        f"- 作成日時（UTC）: {report['generated_at']}",
        f"- 保存範囲: {c['first_record']} ～ {c['last_record']}",
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
        f"換算済み {report['cost']['priced_requests']} / 未換算 {report['cost']['unpriced_requests']} 応答。未換算分は0円ではありません。", "",
        "## 改善候補", ""]
    for f in report["findings"]:
        lines.extend(["### " + f["title"], "", "観測: " + f["observation"], "", "解釈: " + f["interpretation"],
                      "", "次の検証: " + f["action"], ""])
        if f["evidence"]:
            lines += ["根拠: " + ", ".join(f"{e['file']}:L{e['line']}" for e in f["evidence"]), ""]
    if not report["findings"]:
        lines += ["設定した検出条件に該当しません。無駄がないことを保証するものではありません。", ""]
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
    scope += '</section>'
    return '''<!doctype html>
<html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'">
<title>Claude Code 履歴監査</title><style>
:root{color-scheme:light;--ink:#172d31;--muted:#597073;--line:#d9e2de;--paper:#f4f6f2;--accent:#297969}*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:15px/1.8 -apple-system,BlinkMacSystemFont,"Hiragino Kaku Gothic ProN",Meiryo,sans-serif}main{max-width:1200px;margin:auto;padding:46px 32px 70px}header{border-bottom:1px solid var(--line);padding-bottom:27px}.eyebrow{letter-spacing:.16em;font-size:12px;color:var(--accent);font-weight:700}h1{font-size:34px;letter-spacing:-.04em;line-height:1.4;margin:12px 0}h2{font-size:20px;margin:0 0 17px}h3{font-size:17px;margin:0}.muted,small{color:var(--muted)}header p{margin:8px 0}.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin:26px 0}.stat{background:#fff;border:1px solid var(--line);border-radius:12px;padding:21px}.stat span{color:var(--muted);font-size:13px}.stat strong{display:block;font-size:27px;line-height:1.5;margin:9px 0;overflow-wrap:anywhere}.stat small{font-size:11px;display:block}.grid{display:grid;grid-template-columns:1fr 1fr;gap:20px}.panel{background:#fff;border:1px solid var(--line);border-radius:12px;padding:25px;margin-bottom:20px}.bar-row{display:grid;grid-template-columns:120px 1fr 105px;gap:12px;align-items:center;margin:14px 0;font-size:13px}.bar-row b{text-align:right;font-variant-numeric:tabular-nums}.track{height:13px;border-radius:3px;background:#edf1ed;overflow:hidden}.track i{height:100%;display:block}.notice{background:#edf4f0;padding:17px;border-left:3px solid var(--accent);font-size:13px}.finding{border-top:1px solid var(--line);padding:22px 0}.finding:first-of-type{border-top:0;padding-top:0}.finding:last-child{padding-bottom:0}.finding-head{display:flex;gap:12px;align-items:center}.tag{font-size:11px;color:#775420;background:#f7eedc;border-radius:4px;padding:2px 8px;white-space:nowrap}.finding p{margin:8px 0;font-size:14px}.finding p b{margin-right:8px}details{font-size:12px;color:var(--muted)}summary{cursor:pointer}code{font-family:ui-monospace,SFMono-Regular,monospace;font-size:12px;overflow-wrap:anywhere}.scroll{overflow-x:auto}.scroll table{min-width:640px}table{width:100%;border-collapse:collapse;text-align:left;font-size:13px}th{color:var(--muted);font-weight:500;font-size:12px}td,th{padding:12px 10px;border-bottom:1px solid var(--line);vertical-align:top}td small{display:block;font-size:11px}input{font:inherit;padding:9px 13px;border:1px solid var(--line);border-radius:7px;max-width:100%;width:340px;margin-bottom:15px}ul{padding-left:21px;font-size:13px}footer{font-size:12px;color:var(--muted)}.meta{font-size:12px}.metrics{display:flex;gap:25px;flex-wrap:wrap}.metrics strong{font-size:25px;display:block}.metrics span{font-size:12px;color:var(--muted)}@media(max-width:900px){.cards{grid-template-columns:repeat(2,1fr)}.grid{grid-template-columns:1fr}}@media(max-width:550px){main{padding:24px 15px}.cards{gap:8px}.stat{padding:14px}.stat strong{font-size:22px}h1{font-size:27px}.panel{padding:18px}.bar-row{grid-template-columns:90px 1fr 80px;gap:7px;font-size:11px}}@media print{body{background:#fff}main{max-width:none;padding:0}.panel,.stat{break-inside:avoid}.cards{grid-template-columns:repeat(4,1fr)}input{display:none}}
</style></head><body><main><header><div class="eyebrow">CLAUDE HISTORY AUDIT · LOCAL REPORT</div><h1>履歴から、次の改善を見つける。</h1><p>保存済みの作業を集計した監査レポート。観測と改善仮説を分けて確認します。</p><p class="meta">''' + esc(c["first_record"]) + ' → ' + esc(c["last_record"]) + '（UTC） · 作成 ' + esc(report["generated_at"]) + '</p></header>' + scope + '<div class="cards">' + cards + '''</div><div class="grid"><section class="panel"><h2>トークンの内訳</h2>''' + bars + '''<p class="muted meta">読み出しは低単価のキャッシュを含みます。トークン数と費用は比例しません。</p></section><section class="panel"><h2>処理とカバレッジ</h2><div class="metrics">''' + ''.join(f'<div><strong>{fmt(v)}</strong><span>{esc(k)}</span></div>' for k, v in [("対象ファイル", c["files_found"]), ("ツール実行", m["tool_calls"]), ("明示的な失敗", m["tool_errors"])]) + '</div><p class="meta">使用量が揃う応答 ' + f'{c["complete_usage_requests"]} / {c["unique_requests"]}' + ' · 重複レコード ' + str(c.get("duplicate_usage_records", 0)) + '</p><div class="notice">' + esc(report["cost"]["basis"]) + f'<br>換算済み {report["cost"]["priced_requests"]} / 未換算 {report["cost"]["unpriced_requests"]}。未換算分は0円ではありません。' + '</div></section></div><section class="panel"><h2>根拠付きの改善候補</h2>' + findings + '</section><section class="panel"><h2>モデル別の利用</h2><div class="scroll"><table><thead><tr><th>モデル</th><th>応答</th><th>総トークン</th><th>出力</th></tr></thead><tbody>' + model_rows + '</tbody></table></div></section><section class="panel"><h2>セッション別の利用</h2><p class="meta muted">総トークン順、最大200件。作業分類はキーワードによる参考値。全件は report.json に保存しています。</p><input id="filter" aria-label="セッションを絞り込む" placeholder="分類・セッションIDで絞り込み"><div class="scroll"><table id="sessions"><thead><tr><th>セッション / 開始UTC</th><th>作業の参考分類</th><th>応答</th><th>総トークン</th><th>再読込</th><th>失敗</th></tr></thead><tbody>' + session_rows + '</tbody></table></div></section><section class="panel"><h2>この監査で判断できないこと</h2><ul>' + limitations + '</ul><details><summary>欠損・除外を含む集計の詳細</summary><div class="scroll"><table>' + coverage_rows + '</table></div></details></section><footer>このレポート画面は通信しません。会話本文とパスはこの画面に含めません。根拠の実ファイルはローカルの local-map.json で確認できます。</footer></main><script>document.getElementById("filter").addEventListener("input",function(){const q=this.value.toLowerCase();document.querySelectorAll("#sessions tbody tr").forEach(function(row){row.hidden=!row.textContent.toLowerCase().includes(q);});});</script></body></html>'


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
        for name, content in outputs.items():
            fd = os.open(str(staging / name), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(content)
        os.rename(staging, destination)
    return destination

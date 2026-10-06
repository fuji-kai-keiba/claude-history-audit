"""Read untrusted JSONL as data. No source writes, subprocesses, or network IO."""
import hashlib
import hmac
import json
import math
import os
import re
import secrets
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

UTC = timezone.utc
MAX_LINE_BYTES = 8 * 1024 * 1024
TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
TOOLS = {"read": "Read", "write": "Write", "edit": "Edit", "multiedit": "Edit",
         "bash": "Bash", "glob": "Glob", "grep": "Grep", "websearch": "WebSearch",
         "webfetch": "WebFetch", "agent": "Agent", "task": "Agent", "skill": "Skill",
         "notebookedit": "NotebookEdit", "toolsearch": "ToolSearch"}
WORKLOADS = {
    "営業提案": r"提案書|営業資料|商談|sales\s+(?:deck|proposal)|pitch\s+deck",
    "調査レポート": r"調査レポート|市場調査|競合調査|research\s+report|market\s+research",
    "社内会議": r"会議資料|週次報告|月次報告|経営会議|議事録|meeting\s+(?:notes|deck)",
    "資料作成（その他）": r"資料作成|スライド|パワーポイント|powerpoint|presentation|pptx",
    "開発": r"実装|バグ|リファクタ|unittest|refactor|debug|pull\s+request",
}


def parse_time(value):
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.replace(tzinfo=UTC) if result.tzinfo is None else result.astimezone(UTC)
    except (ValueError, OverflowError):
        return None


def iso(value):
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def safe_model(value):
    if isinstance(value, str) and re.fullmatch(r"claude-(?:opus|sonnet|haiku|fable|mythos)-\d+(?:[.-]\d+){0,3}", value):
        return value
    if isinstance(value, str) and re.fullmatch(r"gpt-\d+(?:\.\d+)?(?:-(?:astra|sol|luna|codex|mini|max)){0,2}(?:-\d{4}-\d{2}-\d{2})?", value):
        return value
    return "unknown"


def number(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 10**15 else None


def tool_name(value):
    if not isinstance(value, str):
        return "Other"
    if value.startswith("mcp__"):
        return "MCP"
    return TOOLS.get(value.lower(), "Other")


def percentile(values, fraction):
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


def discover(sources):
    """Do not follow symlink files/directories. Overlapping roots scan each file once."""
    found = {}
    skipped = 0
    errors = 0
    for source in sources:
        source = Path(source).expanduser().absolute()
        if source.is_symlink():
            skipped += 1
            continue
        if source.is_file():
            candidates = [source] if source.suffix == ".jsonl" else []
        elif source.is_dir():
            candidates = []
            def onerror(_):
                nonlocal errors
                errors += 1
            for root, dirs, names in os.walk(source, followlinks=False, onerror=onerror):
                for name in list(dirs):
                    if (Path(root) / name).is_symlink():
                        dirs.remove(name)
                        skipped += 1
                candidates.extend(Path(root) / name for name in names
                                  if name.endswith(".jsonl") and name != "history.jsonl")
        else:
            errors += 1
            continue
        for path in candidates:
            if path.is_symlink():
                skipped += 1
                continue
            try:
                stat = path.stat()
                found.setdefault((stat.st_dev, stat.st_ino), path)
            except OSError:
                errors += 1
    return sorted(found.values(), key=str), skipped, errors


def read_records(path, stats):
    """Snapshot file size and cap individual records, including malformed giant lines."""
    try:
        with path.open("rb") as stream:
            remaining = os.fstat(stream.fileno()).st_size
            line_no = 0
            while remaining > 0:
                data = stream.readline(min(MAX_LINE_BYTES + 1, remaining))
                if not data:
                    break
                remaining -= len(data)
                line_no += 1
                stats["lines_seen"] += 1
                if len(data) > MAX_LINE_BYTES:
                    stats["oversized_lines"] += 1
                    while not data.endswith(b"\n") and remaining > 0:
                        data = stream.readline(min(MAX_LINE_BYTES + 1, remaining))
                        if not data:
                            break
                        remaining -= len(data)
                    continue
                try:
                    row = json.loads(data)
                    if not isinstance(row, dict):
                        raise ValueError("not an object")
                except (ValueError, UnicodeError, RecursionError):
                    stats["malformed_lines"] += 1
                    continue
                yield line_no, row
    except OSError:
        stats["unreadable_files"] += 1


def load_prices(path):
    if not path:
        return None
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("models"), dict):
        raise ValueError("price book must contain models")
    for model, rates in data["models"].items():
        if safe_model(model) == "unknown" or not isinstance(rates, dict):
            raise ValueError("invalid price model")
        fields = ("input", "output", "cache_read", "cache_write") if model.startswith('gpt-') else ("input", "output", "cache_read", "cache_write_5m", "cache_write_1h")
        for field in fields + tuple(k for k in ('long_context_threshold','long_input_multiplier','long_output_multiplier') if k in rates):
            value = rates.get(field)
            if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value < 0:
                raise ValueError("invalid price rate: " + field)
    return data


def cost_components(request, prices):
    """Never guess prices for unknown model IDs, modes, or incomplete usage."""
    if not prices or not request["complete"] or request["nonstandard"]:
        return None
    rates = prices["models"].get(request["model"])
    if not rates:
        return None
    usage = request["usage"]
    multiplier, output_multiplier = 1, 1
    if request.get('provider') == 'codex':
        size = sum(usage[f] for f in TOKEN_FIELDS if f != 'output_tokens')
        if rates.get('long_context_threshold') is not None and size > rates['long_context_threshold']:
            multiplier = rates.get('long_input_multiplier',1)
            output_multiplier = rates.get('long_output_multiplier',1)
        if 'cache_write' not in rates:
            return None
        write = [usage['cache_creation_input_tokens']*rates['cache_write']*multiplier/1e6]*2
    else:
        if 'cache_write_5m' not in rates or 'cache_write_1h' not in rates:
            return None
        total = usage["cache_creation_input_tokens"]
        short = min(request["cache_5m"], total)
        long = min(request["cache_1h"], max(0, total - short))
        known = short * rates["cache_write_5m"] + long * rates["cache_write_1h"]
        unknown = total - short - long
        write = [(known + unknown * rate)/1e6 for rate in (min(rates['cache_write_5m'],rates['cache_write_1h']),max(rates['cache_write_5m'],rates['cache_write_1h']))]
    return {'input':[usage['input_tokens']*rates['input']*multiplier/1e6]*2,
            'cache_read':[usage['cache_read_input_tokens']*rates['cache_read']*multiplier/1e6]*2,
            'output':[usage['output_tokens']*rates['output']*output_multiplier/1e6]*2,'cache_write':write}


def estimate(request, prices):
    parts = cost_components(request, prices)
    return [sum(v[i] for v in parts.values()) for i in (0,1)] if parts else None


def audit(sources, since=None, until=None, prices=None, now=None, identity_key=None, deep=False, provider='auto'):
    if provider not in ('auto','claude','codex'):
        raise ValueError('unknown history provider')
    from .codex import records
    now = now or datetime.now(UTC)
    files, skipped, source_errors = discover(sources)
    if not files:
        raise ValueError("対象のJSONL履歴が見つかりません。--source で保存先を指定してください。")
    if identity_key is not None and (not isinstance(identity_key, bytes) or len(identity_key) != 32):
        raise ValueError("同期用識別キーの形式が不正です。")
    salt = identity_key or secrets.token_bytes(32)
    def alias(kind, raw):
        digest = hmac.new(salt, str(raw).encode(), hashlib.sha256).hexdigest()
        return kind + "-" + (digest if identity_key else digest[:12])
    from .deep import DeepAudit
    detail = DeepAudit(alias) if deep else None
    stats = Counter(files_found=len(files), skipped_symlinks=skipped, source_errors=source_errors)
    requests = {}
    sessions = {}
    user_seen = set()
    tool_seen = set()
    result_seen = set()
    compaction_seen = set()
    tools = []
    errors = []
    local_map = {"sources": [str(Path(p).expanduser().absolute()) for p in sources], "files": {}, "sessions": {}}
    for path in files:
        fid = alias("file", path)
        local_map["files"][fid] = str(path)
        for line, row in records(path, stats, read_records, provider):
            if row.get("type") not in ("assistant", "user", "system"):
                continue
            ts = parse_time(row.get("timestamp"))
            if ts is None:
                stats["missing_timestamps"] += 1
                continue
            if (since and ts < since) or (until and ts >= until):
                stats["outside_window"] += 1
                continue
            stats["records_in_window"] += 1
            raw_session = row.get("sessionId")
            if not isinstance(raw_session, str) or not raw_session:
                raw_session = str(path)
                stats["missing_session_ids"] += 1
            codex = row.get('_codex')
            native = codex is not None
            is_subagent = codex['subagent'] if native else "subagents" in path.parts
            execution = raw_session + ":agent:" + path.stem if is_subagent and not native else raw_session
            sid = alias("session-codex" if native else "session", execution)
            if detail:
                detail.session(sid, raw_session, is_subagent, path)
                if native:
                    detail.executions[sid] = {'subagent':is_subagent,
                        'candidates':{(alias('session-codex',codex['parent']),'explicit_codex_parent')} if codex['parent'] else set()}
            local_map["sessions"][sid] = raw_session
            session = sessions.setdefault(sid, {"id": sid, "first": ts, "last": ts, "prompts": 0,
                "workloads": Counter(), "compactions": 0, "subagent": is_subagent,
                "tools": Counter(), "errors": 0, "read_repeats": 0})
            session["first"] = min(session["first"], ts)
            session["last"] = max(session["last"], ts)
            evidence = {"file": fid, "line": line}
            if row.get("type") == "system" and row.get("subtype") in ("compact_boundary", "microcompact_boundary"):
                marker = row.get("uuid")
                ckey = marker if isinstance(marker, str) else fid + ":" + str(line)
                if ckey not in compaction_seen:
                    compaction_seen.add(ckey)
                    session["compactions"] += 1
                    if detail:
                        detail.compactions.append({"session": sid, "timestamp": iso(ts), "evidence": evidence})
            message = row.get("message")
            if not isinstance(message, dict):
                continue
            content = message.get("content", [])
            blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content
            if not isinstance(blocks, list):
                blocks = []
            mid = message.get("id")
            rid = row.get("requestId")
            if isinstance(mid, str) and mid:
                key = "message:" + mid
            elif isinstance(rid, str) and rid:
                key = "request:" + rid
            else:
                key = "record:" + fid + ":" + str(line)
            if native:
                key = 'codex:'+key
            if row.get("type") == "assistant" and message.get("role", "assistant") == "assistant":
                usage = message.get("usage")
                if not isinstance(usage, dict):
                    stats["assistant_records_without_usage"] += 1
                else:
                    if key.startswith("record:"):
                        stats["requests_without_stable_id"] += 1
                    parsed = {field: number(usage.get(field)) for field in TOKEN_FIELDS}
                    complete = all(value is not None for value in parsed.values()) and (not native or codex.get('complete',False))
                    if not complete:
                        stats["incomplete_usage_records"] += 1
                    cache = usage.get("cache_creation")
                    cache = cache if isinstance(cache, dict) else {}
                    short = number(cache.get("ephemeral_5m_input_tokens")) or 0
                    long = number(cache.get("ephemeral_1h_input_tokens")) or 0
                    if short + long > (parsed["cache_creation_input_tokens"] or 0):
                        stats["inconsistent_cache_splits"] += 1
                        short = long = 0
                    nonstandard = (usage.get("speed") not in (None, "standard", "normal")
                        or usage.get("inference_geo") not in (None, "not_available", "global")
                        or usage.get("service_tier") not in (None, "standard"))
                    values = {field: value or 0 for field, value in parsed.items()}
                    candidate = {"id": alias("request", key), "session": sid, "timestamp": iso(ts),
                        "model": safe_model(message.get("model")), "usage": values, "complete": complete,
                        "cache_5m": short, "cache_1h": long, "nonstandard": nonstandard,
                        "evidence": evidence}
                    candidate['provider'] = 'codex' if native else 'claude'
                    if native:
                        candidate.update(codex_usage_complete=complete,reasoning_output_tokens=codex.get('reasoning'),
                            polling=codex.get('polling',False), polling_evidence=[{'file':fid,'line':n} for n in codex.get('polling_lines',[])])
                    if detail:
                        detail.owners[candidate["id"]].add(sid)
                    if key not in requests:
                        requests[key] = candidate
                    else:
                        stats["duplicate_usage_records"] += 1
                        previous = requests[key]
                        if native:
                            differs = previous['usage'] != values or previous['model'] != candidate['model']
                            conflict = previous.get('codex_conflicting_duplicate',False) or differs
                            stats['codex_conflicting_duplicates'] += int(differs)
                            polling = previous['polling'] and candidate['polling']
                            nonstandard = previous['nonstandard'] or candidate['nonstandard']
                            # Choose one intact observation, preferring complete counters.
                            # Per-component maxima can invent input never sent to a model.
                            def rank(r):
                                return (r['codex_usage_complete'],sum(r['usage'].values()),
                                        tuple(r['usage'][f] for f in TOKEN_FIELDS),r['model'])
                            if rank(candidate) > rank(previous):
                                previous.update(candidate)
                            previous.update(codex_conflicting_duplicate=conflict,
                                complete=previous['codex_usage_complete'] and not conflict,
                                polling=polling,nonstandard=nonstandard)
                        else:
                            # Claude streamed blocks repeat cumulative counters.
                            previous["usage"] = {f: max(previous["usage"][f], values[f]) for f in TOKEN_FIELDS}
                            previous["complete"] |= complete
                            previous["cache_5m"] = max(previous["cache_5m"], short)
                            previous["cache_1h"] = max(previous["cache_1h"], long)
                            previous["nonstandard"] |= nonstandard
            if row.get("type") == "user" and not row.get("isMeta"):
                texts = [b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)]
                if texts:
                    uid = row.get("uuid")
                    ukey = "uuid:" + uid if isinstance(uid, str) else fid + ":" + str(line)
                    if ukey not in user_seen:
                        user_seen.add(ukey)
                        session["prompts"] += 1
                        text_content = "\n".join(texts)
                        for label, pattern in WORKLOADS.items():
                            if re.search(pattern, text_content, re.IGNORECASE):
                                session["workloads"][label] += 1
            for index, block in enumerate(blocks):
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    tid = block.get("id")
                    tkey = tid if isinstance(tid, str) and tid else key + ":" + str(index)
                    if tkey in tool_seen:
                        continue
                    tool_seen.add(tkey)
                    name = tool_name(block.get("name"))
                    session["tools"][name] += 1
                    inputs = block.get("input")
                    inputs = inputs if isinstance(inputs, dict) else {}
                    target = inputs.get("file_path", inputs.get("path", ""))
                    target = target if isinstance(target, str) else ""
                    tools.append({"session": sid, "name": name, "target": alias("document", target) if target else None,
                        "range": str((inputs.get("offset"), inputs.get("limit"), inputs.get("pages"))),
                        "timestamp": ts, "evidence": evidence,
                        "pdf": target.lower().endswith(".pdf")})
                    if detail:
                        detail.tool(tkey, block.get("name"), inputs, sid, iso(ts), evidence)
                if block.get("type") == "tool_result" and detail:
                    detail.result(block, sid, iso(ts), evidence)
                if block.get("type") == "tool_result" and block.get("is_error") is True:
                    tid = block.get("tool_use_id")
                    rkey = tid if isinstance(tid, str) and tid else fid + ":" + str(line) + ":" + str(index)
                    if rkey not in result_seen:
                        result_seen.add(rkey)
                        session["errors"] += 1
                        errors.append(evidence)
    if not stats["records_in_window"]:
        raise ValueError("指定期間に時刻付きのセッション記録がありません。--all または --source を確認してください。")
    request_rows = sorted(requests.values(), key=lambda r: (parse_time(r["timestamp"]), r["id"]))
    for request in request_rows:
        request["cost_usd_range"] = estimate(request, prices)
    totals = {field: sum(r["usage"][field] for r in request_rows) for field in TOKEN_FIELDS}
    context_sizes = [sum(r["usage"][f] for f in TOKEN_FIELDS if f != "output_tokens") for r in request_rows]
    total_input = sum(totals[f] for f in TOKEN_FIELDS if f != "output_tokens")
    repeats = []
    reads = {}
    epochs = Counter()
    for tool in sorted(tools, key=lambda t: (t["timestamp"], t["evidence"]["line"])):
        identity = (tool["session"], tool["target"])
        if tool["name"] in ("Edit", "Write"):
            epochs[identity] += 1
        if tool["name"] != "Read" or not tool["target"]:
            continue
        key = identity + (tool["range"], epochs[identity])
        if key in reads:
            sessions[tool["session"]]["read_repeats"] += 1
            repeats.append({"session": tool["session"], "document": tool["target"], "evidence": tool["evidence"]})
        else:
            reads[key] = tool
    model_rows = []
    for model in sorted({r["model"] for r in request_rows}):
        subset = [r for r in request_rows if r["model"] == model]
        known = [r["cost_usd_range"] for r in subset if r["cost_usd_range"] is not None]
        model_rows.append({"model": model, "requests": len(subset),
            "tokens": {f: sum(r["usage"][f] for r in subset) for f in TOKEN_FIELDS},
            "priced_requests": len(known),
            "cost_usd_range": [sum(x[i] for x in known) for i in (0, 1)] if known else None})
    session_rows = []
    by_session = defaultdict(list)
    for request in request_rows:
        by_session[request["session"]].append(request)
    for sid, session in sessions.items():
        subset = by_session[sid]
        labels = session["workloads"]
        session_rows.append({"id": sid, "first": iso(session["first"]), "last": iso(session["last"]),
            "requests": len(subset), "prompts": session["prompts"], "subagent": session["subagent"],
            "workload_hint": max(labels, key=labels.get) if labels else "不明",
            "tokens": sum(sum(r["usage"].values()) for r in subset),
            "tools": dict(session["tools"]), "tool_errors": session["errors"],
            "repeated_reads": session["read_repeats"], "compactions": session["compactions"]})
    session_rows.sort(key=lambda s: (-s["tokens"], s["id"]))
    findings = []
    def finding(code, title, observation, interpretation, action, evidence, severity="check"):
        findings.append({"code": code, "severity": severity, "title": title, "observation": observation,
            "interpretation": interpretation, "action": action, "evidence": evidence[:5]})
    if len(request_rows) < 20 or len(session_rows) < 3:
        finding("limited_sample", "監査範囲が小さい", f"{len(session_rows)}セッション・{len(request_rows)}リクエストを確認。",
            "この保存範囲から会社全体の利用傾向や削減率は判断できません。", "他端末・他保存先と対象期間の利用CSVを確認する。", [], "info")
    if repeats:
        finding("repeated_reads", "同じ範囲の読み直し", f"同一セッション内でReadの対象・範囲が重なる呼び出しが{len(repeats)}件。",
            "再利用できる可能性があります。Bash・外部編集は追跡していないため、不要な再読込とは断定できません。",
            "該当箇所を確認し、抽出済みデータや根拠メモの再利用を比較する。", [r["evidence"] for r in repeats])
    if errors:
        finding("tool_errors", "ツールの失敗", f"is_error=true のツール結果が{len(errors)}件。",
            "生成・変換処理の手戻り候補です。失敗が必要な探索だった可能性もあります。",
            "失敗ツールを確認し、定型処理・入力検証・生成テンプレートを整える。", errors)
    large = [r for r, size in zip(request_rows, context_sizes) if size >= 50000]
    if large:
        finding("large_context", "大きい入力コンテキスト", f"入力合計が50,000トークン以上のリクエストが{len(large)}件。",
            "50,000は調査の目安であり、過剰利用の判定基準ではありません。キャッシュ済み入力も含みます。",
            "必要なページ・表だけを渡す方式を、品質とキャッシュ費用を含めて比較する。", [r["evidence"] for r in large])
    if total_input and totals["cache_creation_input_tokens"] / total_input >= .25:
        heavy = sorted(request_rows, key=lambda r: r["usage"]["cache_creation_input_tokens"], reverse=True)
        finding("cache_writes", "キャッシュ書き込みの比率", f"入力トークンの{totals['cache_creation_input_tokens'] / total_input:.1%}がキャッシュ書き込み。",
            "新規作業・有効期間・入力変更など複数の原因があり、キャッシュ失敗率を意味しません。",
            "リクエスト間隔と入力変更を調べ、同じ作業条件で有効期間や情報配置を比較する。", [r["evidence"] for r in heavy])
    compactions = sum(s["compactions"] for s in session_rows)
    if compactions:
        finding("compaction", "会話の圧縮記録", f"圧縮境界の記録が{compactions}件。",
            "圧縮自体は正常な動作です。圧縮後の情報の再取得や手戻りは追加確認が必要です。",
            "決定事項・根拠ID・未完了事項を明示した引き継ぎを試す。", [], "info")
    known = [r["cost_usd_range"] for r in request_rows if r["cost_usd_range"] is not None]
    coverage = dict(stats)
    coverage.update(unique_requests=len(request_rows), complete_usage_requests=sum(r["complete"] for r in request_rows),
        sessions=len(session_rows), priced_requests=len(known),
        first_record=min((s["first"] for s in session_rows), default=None),
        last_record=max((s["last"] for s in session_rows), default=None))
    report = {"schema_version": 1, "generated_at": iso(now),
        "window": {"since": iso(since) if since else None, "until_exclusive": iso(until) if until else None},
        "coverage": coverage, "totals": totals,
        "metrics": {"input_total": total_input, "cache_read_token_share": totals["cache_read_input_tokens"] / total_input if total_input else None,
            "context_p50": percentile(context_sizes, .5), "context_p95": percentile(context_sizes, .95),
            "tool_calls": len(tools), "tool_errors": len(errors), "repeated_reads": len(repeats),
            "pdf_read_calls": sum(t["name"] == "Read" and t["pdf"] for t in tools)},
        "providers": sorted({r.get('provider','claude') for r in request_rows}),
        "cost": {"basis": "API単価による参考額。実請求・固定席代・税・外部ツール費用は含まない。Codexの速度記録がない場合は標準速度の仮定であり、契約枠の消費量ではない。",
            "price_book_as_of": prices.get("as_of") if prices and isinstance(prices.get("as_of"), str)
                and re.fullmatch(r"\d{4}-\d{2}-\d{2}", prices["as_of"]) else None,
            "enabled": prices is not None, "priced_requests": len(known), "unpriced_requests": len(request_rows) - len(known),
            "usd_range": [sum(x[i] for x in known) for i in (0, 1)] if known else None},
        "models": model_rows, "sessions": session_rows, "findings": findings, "requests": request_rows,
        "limitations": [
            "Codexは応答別usageを優先。旧token_countは差分と最終応答が一致する記録だけを採用し、曖昧・欠損は件数を表示。キャッシュと推論は入力・出力の内数。ClaudeのTTLをCodexへ適用しません。",
            "Codexの複合ツール結果の成否は不明として保持。ツール失敗0は成功の証明ではありません。巨大行の除外はカバレッジを参照。",
            "取得・指定できた保存ログだけを分析。未登録端末・Web・Cowork・削除済み履歴・固定席代を自動で網羅しません。",
            "内部JSONL形式はバージョンで変化します。欠損・不明項目はカバレッジに表示します。",
            "Claudeの同じmessage.id（なければrequestId）は各カウンターの最大値で集約。Codexの同じresponse_idは欠損の少ない単一記録を優先し、矛盾は未換算。識別子のない記録は重複排除を保証しません。",
            "記録時刻のない行は期間集計から除外。サブエージェントの記録は保存範囲にある場合だけ含めます。",
            "作業分類はユーザー文章のキーワードによる参考値。品質・人間の修正時間・確定削減額は履歴だけでは測定できません。",
            "改善候補は観測事実と仮説を分けて表示。キャッシュ再利用率はトークン比率であり、リクエストのヒット率ではありません。",
        ]}
    if detail:
        report["deep"] = detail.build(report, prices, by_session)
    return report, local_map

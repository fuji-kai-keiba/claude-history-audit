"""Why tokens are spent: a mechanism model of Claude Code requests. No IO, aggregates only.

Every API request resends the whole context. So a token's cost = its size x the number of later
requests it stays in context. This module rebuilds each execution's request sequence, estimates
what was appended between requests (own output, tool results by tool, user/system text, images),
fits how much of each enters the next context, and attributes cache reads to those origins.
Attribution is an estimate; reconciliation, fit quality, and split-half stability are reported.
"""
import hashlib
import json
import statistics
from collections import Counter, defaultdict

from .audit import TOKEN_FIELDS, parse_time, tool_name

FEATURES = ("previous_output_tokens", "previous_visible_bytes", "tool_result_bytes", "user_system_bytes", "images", "constant")
MIN_FIT_ROWS = 200
MAX_DELTA = 300000
MAX_FIT_GAP = 3000
RESET_RATIO = 0.7


def blen(text):
    return len(text.encode("utf-8")) if isinstance(text, str) else 0


def result_size(content):
    if isinstance(content, str):
        return blen(content), 0
    size = images = 0
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    size += blen(block.get("text"))
                elif block.get("type") == "image":
                    images += 1
    return size, images


def solve(matrix, vector):
    n = len(vector)
    a = [row[:] + [vector[i]] for i, row in enumerate(matrix)]
    for i in range(n):
        pivot = max(range(i, n), key=lambda r: abs(a[r][i]))
        if abs(a[pivot][i]) < 1e-9:
            return None
        a[i], a[pivot] = a[pivot], a[i]
        for r in range(n):
            if r != i:
                f = a[r][i] / a[i][i]
                a[r] = [x - f * y for x, y in zip(a[r], a[i])]
    return [a[i][n] / a[i][i] for i in range(n)]


class Fit:
    def __init__(self):
        k = len(FEATURES)
        self.xtx = [[0.0] * k for _ in range(k)]
        self.xty = [0.0] * k
        self.rows = 0

    def add(self, x, y):
        for i, xi in enumerate(x):
            self.xty[i] += xi * y
            row = self.xtx[i]
            for j, xj in enumerate(x):
                row[j] += xi * xj
        self.rows += 1

    def coefficients(self):
        """Columns without variance (e.g. no images) are fixed at 0 instead of making the system singular."""
        if self.rows < MIN_FIT_ROWS:
            return None
        n, k = self.rows, len(FEATURES)
        const = k - 1
        keep = [i for i in range(k) if i == const
                or self.xtx[i][i] / n - (self.xtx[i][const] / n) ** 2 > 1e-9 * max(1.0, self.xtx[i][i] / n)]
        sub = solve([[self.xtx[i][j] for j in keep] for i in keep], [self.xty[i] for i in keep])
        if sub is None:
            return None
        coef = [0.0] * k
        for i, v in zip(keep, sub):
            coef[i] = v
        return coef


class Anatomy:
    def __init__(self, alias):
        self.alias = alias
        self.streams = {}
        self.tool_names = {}
        self.cwds = {}
        self.inside, self.started_outside = set(), set()

    def outside(self, fid):
        """A file whose first rows fall before the window starts mid-session (carried-in context)."""
        if fid not in self.inside:
            self.started_outside.add(fid)

    def stream(self, fid, sid, subagent):
        key = (fid, sid)
        if key not in self.streams:
            self.streams[key] = {"sid": sid, "fid": fid, "subagent": subagent, "items": [], "index": {},
                                 "pending": Counter(), "images": 0}
        return self.streams[key]

    def row(self, fid, sid, subagent, row, key, blocks):
        """Feed every Claude row in file order (after window filtering)."""
        self.inside.add(fid)
        cwd = row.get("cwd")
        if isinstance(cwd, str) and cwd and sid not in self.cwds:
            self.cwds[sid] = cwd
        s = self.stream(fid, sid, subagent)
        kind = row.get("type")
        if kind == "assistant":
            message = row.get("message") or {}
            if not isinstance(message.get("usage"), dict):
                return
            if key not in s["index"]:
                s["index"][key] = len(s["items"])
                s["items"].append({"key": key, "ts": row.get("timestamp"), "appended": s["pending"],
                                   "images": s["images"], "visible": 0, "tools": 0})
                s["pending"], s["images"] = Counter(), 0
            item = s["items"][s["index"][key]]
            if s["index"][key] != len(s["items"]) - 1:
                return
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    item["visible"] += blen(block.get("text"))
                elif block.get("type") == "tool_use":
                    item["tools"] += 1
                    try:
                        item["visible"] += len(json.dumps(block.get("input"), ensure_ascii=False).encode("utf-8"))
                    except (TypeError, ValueError):
                        pass
                    raw = block.get("name")
                    self.tool_names[block.get("id")] = "PowerShell" if isinstance(raw, str) and raw.lower() == "powershell" else tool_name(raw)
        elif kind == "user":
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_result":
                    size, images = result_size(block.get("content"))
                    s["pending"]["tool:" + self.tool_names.get(block.get("tool_use_id"), "Other")] += size
                    s["images"] += images
                elif block.get("type") == "text":
                    s["pending"]["user_system"] += blen(block.get("text"))
                elif block.get("type") == "image":
                    s["images"] += 1

    def build(self, requests, prices, sessions, config=None):
        rates = (prices or {}).get("models", {}) if prices else {}
        best = {}
        for (fid, sid), s in self.streams.items():
            if sid not in best or len(s["items"]) > len(best[sid]["items"]):
                best[sid] = s
        seqs, consumed, dropped = [], set(), 0
        def first_ts(s):
            t = parse_time(s["items"][0]["ts"]) if s["items"] else None
            return (t is None, t.isoformat() if t else "", s["sid"])
        for s in sorted(best.values(), key=first_ts):
            seq = []
            for position, item in enumerate(s["items"]):
                req = requests.get(item["key"])
                if not req or req.get("provider", "claude") != "claude":
                    continue
                u = req["usage"]
                ctx = u["input_tokens"] + u["cache_creation_input_tokens"] + u["cache_read_input_tokens"]
                if ctx <= 0 or req["model"] == "unknown":
                    dropped += 1  # synthetic/error responses carry no context
                    continue
                # Forked/resumed copies repeat requests under another session: count each request once,
                # keep copies only for context continuity.
                counted = item["key"] not in consumed
                consumed.add(item["key"])
                rate = rates.get(req["model"], {}) if req["complete"] and not req["nonstandard"] else {}
                seq.append({"item": item, "req": req, "ctx": ctx, "count": counted, "position": position,
                            "ts": parse_time(item["ts"]) or parse_time(req["timestamp"]),
                            "read_rate": rate.get("cache_read"), "r5": rate.get("cache_write_5m"), "r1": rate.get("cache_write_1h")})
            if any(x["count"] for x in seq):
                true_start = seq[0]["count"] and seq[0]["position"] == 0 and s["fid"] not in self.started_outside
                seqs.append((s["sid"], s["subagent"], seq, true_start))
        all_claude = [r for r in requests.values() if r.get("provider", "claude") == "claude"]
        total_read_all = sum(r["usage"]["cache_read_input_tokens"] for r in all_claude)
        if not seqs:
            return {"status": "no_claude_sequences"}

        # 1. Fit: next-context growth from what was appended.
        fit, halves, rows = Fit(), (Fit(), Fit()), []
        for sid, _, seq, _ in seqs:
            half = int(hashlib.sha256(sid.encode()).hexdigest(), 16) % 2
            for prev, cur in zip(seq, seq[1:]):
                if not cur["count"]:
                    continue
                delta = cur["ctx"] - prev["ctx"]
                gap = (cur["ts"] - prev["ts"]).total_seconds() if cur["ts"] and prev["ts"] else None
                if delta <= 0 or delta > MAX_DELTA or gap is None or gap > MAX_FIT_GAP:
                    continue
                app = cur["item"]["appended"]
                x = (prev["req"]["usage"]["output_tokens"], prev["item"]["visible"],
                     sum(v for k, v in app.items() if k.startswith("tool:")), app.get("user_system", 0),
                     cur["item"]["images"], 1.0)
                fit.add(x, delta)
                halves[half].add(x, delta)
                rows.append((x, delta))
        coef = fit.coefficients()
        fit_info = {"rows": fit.rows, "features": list(FEATURES), "coefficients": None, "r2": None,
                    "split_half": [h.coefficients() for h in halves], "status": "insufficient_data"}
        if coef:
            mean = sum(y for _, y in rows) / len(rows)
            ss_tot = sum((y - mean) ** 2 for _, y in rows)
            ss_res = sum((y - sum(c * xi for c, xi in zip(coef, x))) ** 2 for x, y in rows)
            fit_info.update(coefficients=dict(zip(FEATURES, coef)), r2=1 - ss_res / ss_tot if ss_tot else None, status="fitted")
            def close(a, b):
                return abs(a - b) <= 0.2 * max(abs(b), 0.05)
            stable = all(h and close(h[0], coef[0]) and close(h[2], coef[2]) for h in fit_info["split_half"])
            fit_info["stable"] = stable
        c = [max(0.0, v) for v in coef] if coef else None

        # 2. Attribute cache reads to origins alive in context.
        tokens, usd, priced_read_tokens = Counter(), Counter(), 0
        covered_read = 0
        startup_parent, startup_sub = [], []
        reuse_den = 0
        for sid, subagent, seq, true_start in seqs:
            if true_start:
                (startup_sub if subagent else startup_parent).append(seq[0]["ctx"])
            reuse_den += seq[0]["ctx"]
            alive, base, prev = [], seq[0]["ctx"], None
            base_kind = "startup" if true_start else "carried_in"
            for cur in seq:
                if prev is not None:
                    if cur["ctx"] < prev["ctx"] * RESET_RATIO:
                        alive, base, base_kind = [], cur["ctx"], "carried_in"
                        reuse_den += cur["ctx"]
                    else:
                        delta = max(0, cur["ctx"] - prev["ctx"])
                        reuse_den += delta
                        est = Counter()
                        if c:
                            est["own_output"] = c[0] * prev["req"]["usage"]["output_tokens"] + c[1] * prev["item"]["visible"]
                            for k, v in cur["item"]["appended"].items():
                                if k.startswith("tool:"):
                                    est[k] += c[2] * v
                                else:
                                    est["user_system"] += c[3] * v
                            est["images"] += c[4] * cur["item"]["images"]
                        total = sum(est.values())
                        for k, v in est.items():
                            if total > 0 and v > 0:
                                alive.append((k, delta * v / total))
                        if total <= 0 and delta:
                            alive.append(("unattributed", delta))
                prev = cur
                if not cur["count"]:
                    continue
                read = cur["req"]["usage"]["cache_read_input_tokens"]
                covered_read += read
                hist = sum(t for _, t in alive)
                known = base + hist
                scale = min(1.0, read / known) if known else 0.0
                parts = [(base_kind, base * scale)] + [(k, t * scale) for k, t in alive]
                over = max(0, read - known)
                if over:
                    parts.append(("unattributed", over))
                price = cur["read_rate"]
                for k, t in parts:
                    tokens[k] += t
                    if price is not None:
                        usd[k] += t * price / 1e6
                if price is not None:
                    priced_read_tokens += read
        origins = []
        for k, t in tokens.most_common():
            origins.append({"origin": k, "read_tokens": round(t), "share": t / covered_read if covered_read else None,
                            "usd": usd[k] if priced_read_tokens else None})
        # Allocation sums to the reads by construction; the real check is that no request is counted twice.
        reconciliation = (abs(sum(tokens.values()) - covered_read) <= max(1.0, covered_read * 1e-6)
                          and covered_read <= total_read_all * (1 + 1e-9))

        # 3. Structure of requests.
        reqs = [cur for _, _, seq, _ in seqs for cur in seq if cur["count"]]
        single = sum(1 for r in reqs if r["item"]["tools"] == 1)
        multi = sum(1 for r in reqs if r["item"]["tools"] >= 2)
        claude_sids = {sid for sid, _, _, _ in seqs}
        prompts = sum(s["prompts"] for sid, s in sessions.items() if sid in claude_sids and not s.get("subagent"))
        out_total = sum(r["req"]["usage"]["output_tokens"] for r in reqs)
        visible = sum(r["item"]["visible"] for r in reqs)
        thinking_share = None
        if c and out_total and c[2] > 0:
            thinking_share = max(0.0, min(1.0, 1 - visible * c[2] / out_total))
        structure = {"requests": len(reqs), "executions": len(seqs), "human_prompts": prompts,
                     "requests_per_prompt": len(reqs) / prompts if prompts else None,
                     "single_tool_requests": single, "multi_tool_requests": multi,
                     "reuse_multiplier": covered_read / reuse_den if reuse_den else None,
                     "startup_context_p50": {"parent": statistics.median(startup_parent) if startup_parent else None,
                                             "subagent": statistics.median(startup_sub) if startup_sub else None},
                     "subagent_executions": sum(1 for _, sub, _, _ in seqs if sub), "true_starts": len(startup_parent) + len(startup_sub),
                     "excluded_zero_or_unknown": dropped, "output_tokens": out_total, "visible_output_bytes": visible,
                     "thinking_share_estimate": thinking_share}

        # 4. Cache TTL counterfactual (only the reads/writes recorded here; same request sequence assumed).
        saved = extra = 0.0
        ttl_ok = 0
        w1 = sum(r["req"]["cache_1h"] for r in reqs)
        w5 = sum(r["req"]["cache_5m"] for r in reqs)
        for _, _, seq, _ in seqs:
            for prev, cur in zip([None] + seq, seq):
                if not cur["count"] or cur["r1"] is None or cur["r5"] is None or cur["read_rate"] is None:
                    continue
                ttl_ok += 1
                saved += cur["req"]["cache_1h"] * (cur["r1"] - cur["r5"]) / 1e6
                if prev is not None and cur["ts"] and prev["ts"] and 300 < (cur["ts"] - prev["ts"]).total_seconds() <= 3600:
                    extra += cur["req"]["usage"]["cache_read_input_tokens"] * (cur["r5"] - cur["read_rate"]) / 1e6
        ttl = {"status": "not_applicable" if not w1 or w1 < w5 or not ttl_ok else "estimated",
               "observed_1h_write_share": w1 / (w1 + w5) if w1 + w5 else None,
               "if_5m_write_savings_usd": saved, "if_5m_added_rewrite_usd": extra, "net_usd": extra - saved,
               "assumption": "5分超〜1時間の間隔で再開した応答の読出がすべて5分キャッシュの再書込になり、他の要求の順序と量は変わらないと仮定。"}

        mechanisms = explain(origins, structure, ttl, fit_info, config, requests, sessions, coverage=covered_read / total_read_all if total_read_all else None)
        return {"status": "estimated" if coef else "structure_only", "fit": fit_info, "origins": origins,
                "reconciled": reconciliation, "read_tokens_covered": covered_read, "read_tokens_all": total_read_all,
                "structure": structure, "cache_ttl": ttl, "mechanisms": mechanisms, "config": config,
                "notes": [
                    "内訳は要求間の文脈増分を回帰で配分した推定。1行ごとの実トークン数ではない。",
                    "出力の持ち越し係数は思考を含む出力が次の文脈に残る割合の推定。可視出力の係数が小さく出力の係数が1に近いほど思考も残っている。",
                    "起動時の文脈には Claude Code 本体の指示・ツール定義と、CLAUDE.md・記憶・最初の依頼が含まれる。指示ファイルの量は現在のファイルから測った値。",
                    "TTL試算と単価は標準API参考額。品質・行動が変わる場合の削減額ではない。"]}


def share_of(origins, prefix):
    return sum(o["share"] or 0 for o in origins if o["origin"].startswith(prefix))


def usd_of(origins, prefix):
    vals = [o["usd"] for o in origins if o["origin"].startswith(prefix) and o["usd"] is not None]
    return sum(vals) if vals else None


def explain(origins, structure, ttl, fit, config, requests, sessions, coverage):
    """Map each cost mechanism to whether it is structural, configurable, or behavioral."""
    m = []
    config = config or {}
    env, settings = config.get("env", {}), config.get("settings", {})
    def add(code, title, status, observation, levers, validation, linked=None):
        m.append({"code": code, "title": title, "status": status, "observation": observation,
                  "levers": levers, "linked_settings": linked or {}, "validation": validation})
    reuse = structure["reuse_multiplier"]
    add("resend", "要求ごとに文脈全体を再送する", "structural",
        f"要求 {structure['requests']:,}件。人の発言1回あたり要求 {structure['requests_per_prompt']:.1f}件。文脈に入ったトークンは平均 {reuse:.1f} 回読み直された。" if reuse and structure['requests_per_prompt'] else "要求の再送構造を集計。",
        "API の仕組み。キャッシュで読出単価は入力の1/10以下（モデルにより1/20〜1/40）になっており、これ以上の構造的な削減手段はない。減らせるのは要求の回数と1回の文脈量。",
        "なし（構造）。")
    own = share_of(origins, "own_output")
    coef = (fit.get("coefficients") or {})
    linked = {k: v for k, v in settings.items() if k in ("effortLevel", "model")}
    if config.get("model_effort"):
        linked["modelSettings.effortLevel"] = config["model_effort"]
    if env.get("MAX_THINKING_TOKENS") is not None:
        linked["MAX_THINKING_TOKENS"] = env["MAX_THINKING_TOKENS"]
    ts = structure.get("thinking_share_estimate")
    add("own_output", "自分の出力（思考を含む）の読み直し", "configurable" if coef else "insufficient_data",
        (f"読出の {own:.0%} が過去の出力由来。出力1トークンのうち約 {coef.get('previous_output_tokens', 0):.2f} が次の文脈に残る推定"
         + (f"、出力のうち思考は約 {ts:.0%} と推定。" if ts is not None else "。")) if coef else "回帰に十分な区間がない。",
        "出力を文脈に残すのはハーネス/API の仕様で変えられない。生成量は推論の深さ（effort）で決まり、出力費と以後の読み直しの両方に効く。",
        "同じ種類の作業で effort だけを変え、品質基準を固定して総費用・修正回数を比較する。", linked)
    startup = share_of(origins, "startup")
    p50 = structure["startup_context_p50"]["parent"]
    projects = config.get("projects") or []
    tpb = max(0.0, coef.get("tool_result_bytes") or 0) if coef else None
    weighted = [p["instruction_bytes"] for p in projects for _ in range(max(1, p.get("sessions", 0)))]
    instr = statistics.median(weighted) if weighted else None
    largest = max(projects, key=lambda p: p["instruction_bytes"]) if projects else None
    user_tokens = instr * tpb if instr is not None and tpb else None
    add("startup", "起動時の固定分（本体の指示・ツール定義・指示ファイル）", "partly_configurable",
        f"読出の {startup:.0%}。起動時の文脈の中央値は親 {p50:,.0f} / 子 {structure['startup_context_p50']['subagent'] or 0:,.0f} tokens。"
        + (f" 自分で書いた指示ファイル（CLAUDE.md・取込・記憶の索引）は親セッション加重の中央値 {instr:,.0f} bytes ≒ {user_tokens:,.0f} tokens（推定）。" if user_tokens else "")
        + (f" 最大は {largest['project']} の {largest['instruction_bytes']:,} bytes（" + "、".join(f"{k} {v:,}" for k, v in sorted(largest['by_kind'].items(), key=lambda kv: -kv[1])) + "）。" if largest else "") if p50 else "起動時の文脈を集計できない。",
        "指示ファイル・記憶の索引の分は減らせる。残りは Claude Code 本体の指示とツール定義で、使わないツール・MCP・プラグインを外す以外に手段がない。サブエージェントも起動のたびにこの固定分を持つ。",
        "指示ファイルを分割・必要時読込にした版と比較し、起動時文脈と作業品質を確認する。",
        {"enabled_plugins": config.get("enabled_plugins"), "user_mcp_servers": config.get("user_mcp_servers")} if config else {})
    tools = sorted(((o["origin"][5:], o["share"]) for o in origins if o["origin"].startswith("tool:")), key=lambda x: -(x[1] or 0))
    add("tool_results", "ツール結果の読み直し", "behavioral",
        "読出の " + f"{share_of(origins, 'tool:'):.0%}。" + "、".join(f"{n} {s:.0%}" for n, s in tools[:4] if s) + "。",
        "1回の量は出力上限の設定で既に抑えられる。多いのは回数。大量の探索を別の実行に分けると結果の要約だけが残るが、子は起動時の固定分を払う。古いツール結果を会話から消す設定は確認できていない。",
        "大量の探索を分離した作業と分離しない作業で、総費用と品質を比較する。",
        {k: v for k, v in settings.items() if k in ("bashOutputMaxChars", "taskOutputMaxChars")})
    single, multi = structure["single_tool_requests"], structure["multi_tool_requests"]
    add("request_count", "要求の回数（ツールを1個ずつ呼ぶ）", "behavioral",
        f"ツールを1個だけ呼んだ要求 {single:,}件 / 2個以上まとめた要求 {multi:,}件。",
        "独立した読取・検索を1回の要求にまとめれば、その分の要求と文脈の読み直しが丸ごと消える。モデルの行動なのでルールで促せるが強制はできない。",
        "まとめ呼びを促すルールの前後で、1発言あたり要求数と総費用を比較する。")
    if ttl["status"] == "estimated":
        verdict = "keep_current" if ttl["net_usd"] > 0 else "review"
        add("cache_ttl", "キャッシュ書込の有効期間（1時間は入力の2倍）", verdict,
            f"1時間書込の比率 {ttl['observed_1h_write_share']:.0%}。5分にすると書込は約 ${ttl['if_5m_write_savings_usd']:,.0f} 減るが、5分〜1時間の再開で約 ${ttl['if_5m_added_rewrite_usd']:,.0f} の再書込が増える試算。",
            "差し引きが正なら現行の1時間が有利。1時間を超える休憩後の全文再書込は有効期間の上限による構造で、再開時の文脈を小さくする以外に減らせない。",
            "試算の仮定（同じ要求順序）を保ったまま、期間を変えた短期比較で確認する。",
            {k: v for k, v in list(env.items()) + list(settings.items()) if k in ("promptCacheTtl", "CLAUDE_CODE_PROMPT_CACHE_TTL", "ENABLE_PROMPT_CACHING_1H", "autoCompactWindow", "CLAUDE_CODE_AUTOCOMPACT_PCT_OVERRIDE")})
    sub_cost = Counter()
    for r in requests.values():
        s = sessions.get(r["session"])
        if s and s.get("subagent") and r.get("provider", "claude") == "claude" and r.get("cost_usd_range"):
            sub_cost[r["model"]] += r["cost_usd_range"][0]
    if sub_cost:
        total = sum(sub_cost.values())
        top, val = sub_cost.most_common(1)[0]
        setting = env.get("CLAUDE_CODE_SUBAGENT_MODEL")
        add("subagent_model", "サブエージェントのモデル", "configurable" if setting else "behavioral",
            f"サブエージェントの参考額 ${total:,.0f} のうち {top} が {val / total:.0%}。" + (f" 既定モデルの設定 CLAUDE_CODE_SUBAGENT_MODEL={setting}。" if setting else " 既定モデルの設定なし。"),
            "既定モデルは設定で決まる（呼出時の model 指定が優先）。単価の差はトークン量が同じ場合の比で、品質と再作業は別に確認が必要。",
            "抽出・調査など同種の子作業で既定モデルを変え、品質と総費用を比較する。",
            {"CLAUDE_CODE_SUBAGENT_MODEL": setting, "CLAUDE_CODE_SUBAGENT_MODEL_FORCE": env.get("CLAUDE_CODE_SUBAGENT_MODEL_FORCE")} if config else {})
    for item in m:
        item["coverage_of_reads"] = coverage
    return m


STATUS_LABELS = {"structural": "構造（解消不可）", "configurable": "設定で調整可", "partly_configurable": "一部は設定で調整可",
                 "behavioral": "使い方・ルールで調整可", "keep_current": "現状維持が有利", "review": "見直し候補",
                 "insufficient_data": "データ不足"}
ORIGIN_LABELS = {"startup": "起動時の固定分", "carried_in": "再開・圧縮後に持ち越した文脈", "own_output": "自分の過去の出力（思考を含む）", "user_system": "ユーザー発言・system情報",
                 "images": "画像", "unattributed": "未割当"}


def label(origin):
    return "ツール結果: " + origin[5:] if origin.startswith("tool:") else ORIGIN_LABELS.get(origin, origin)


def anatomy_markdown(a):
    if not a or a.get("status") in (None, "no_claude_sequences"):
        return []
    s, fit = a["structure"], a["fit"]
    lines = ["## なぜトークンがかかるか（仕組みの分解）", "",
             "要求ごとに文脈全体が再送されるため、各トークンの費用は「大きさ × 文脈に残った間の要求回数」で決まる。以下は要求間の文脈増分を発生源に配分した推定。", ""]
    cov = a["read_tokens_covered"] / a["read_tokens_all"] if a["read_tokens_all"] else None
    lines.append(f"- 対象: Claude の要求 {s['requests']:,}件・実行 {s['executions']:,}件。キャッシュ読出の {cov:.0%} を配分。照合: {'一致' if a['reconciled'] else '不一致'}。" if cov is not None else "- 対象を集計できない。")
    if fit.get("coefficients"):
        k = fit["coefficients"]
        r2 = "—" if fit.get("r2") is None else "{:.2f}".format(fit["r2"])
        lines.append(f"- 回帰: 区間 {fit['rows']:,}、決定係数 {r2}、半分割の安定性 {'あり' if fit.get('stable') else 'なし（係数を参考値として扱う）'}。出力の持ち越し係数 {k['previous_output_tokens']:.2f}、ツール結果 1byte あたり {k['tool_result_bytes']:.2f} tokens。")
    lines += ["", "| 発生源 | 読出tokens | 割合 | 読出参考額 |", "|---|---:|---:|---:|"]
    for o in a["origins"][:12]:
        usd = "—" if o["usd"] is None else "${:,.0f}".format(o["usd"])
        lines.append("| {} | {:,} | {:.1%} | {} |".format(label(o["origin"]), o["read_tokens"], o["share"] or 0, usd))
    rp = s["requests_per_prompt"]
    lines += ["", f"人の発言1回あたり要求 {rp:.1f}件、読み直し倍率 {s['reuse_multiplier']:.1f}倍、起動時文脈の中央値 親 {s['startup_context_p50']['parent'] or 0:,.0f} / 子 {s['startup_context_p50']['subagent'] or 0:,.0f} tokens。" if rp and s["reuse_multiplier"] else "", "",
              "### 仕組みごとの解消可能性", "", "| 仕組み | 判定 | 観測 | 手段と限界 |", "|---|---|---|---|"]
    for item in a["mechanisms"]:
        linked = "、".join(f"{k}={v}" for k, v in item["linked_settings"].items() if v is not None)
        lines.append(f"| {item['title']} | {STATUS_LABELS.get(item['status'], item['status'])} | {item['observation']}{('（関連設定: ' + linked + '）') if linked else ''} | {item['levers']} |")
    lines += ["", "設定値はこの端末の現在のもの。"] + ["- " + n for n in a["notes"]] + [""]
    return lines


def anatomy_html(a, esc):
    if not a or a.get("status") in (None, "no_claude_sequences"):
        return ""
    rows = "".join("<tr><td>" + esc(label(o["origin"])) + "</td><td>" + f"{o['read_tokens']:,}" + "</td><td>" + f"{(o['share'] or 0):.1%}"
                   + "</td><td>" + ("—" if o["usd"] is None else f"${o['usd']:,.0f}") + "</td></tr>" for o in a["origins"][:12])
    mech = "".join("<tr><td>" + esc(i["title"]) + "</td><td>" + esc(STATUS_LABELS.get(i["status"], i["status"])) + "</td><td>" + esc(i["observation"])
                   + "</td><td>" + esc(i["levers"]) + "</td></tr>" for i in a["mechanisms"])
    s = a["structure"]
    meta = (f"人の発言1回あたり要求 {s['requests_per_prompt']:.1f}件 · 読み直し倍率 {s['reuse_multiplier']:.1f}倍 · 照合 {'一致' if a['reconciled'] else '不一致'}"
            if s["requests_per_prompt"] and s["reuse_multiplier"] else "")
    return ('<section class="panel"><h2>なぜトークンがかかるか</h2><p class="meta muted">要求ごとに文脈全体が再送される。要求間の文脈増分を発生源に配分した推定。'
            + esc(meta) + '</p><div class="scroll"><table><thead><tr><th>発生源</th><th>読出tokens</th><th>割合</th><th>読出参考額</th></tr></thead><tbody>'
            + rows + '</tbody></table></div><h3>仕組みごとの解消可能性</h3><div class="scroll"><table><thead><tr><th>仕組み</th><th>判定</th><th>観測</th><th>手段と限界</th></tr></thead><tbody>'
            + mech + '</tbody></table></div><p class="meta muted">' + esc(" ".join(a["notes"])) + '</p></section>')

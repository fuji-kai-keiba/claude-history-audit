"""Optional local diagnostics. Retain numerical metadata and salted identities only."""
import json
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict

from .audit import TOKEN_FIELDS, parse_time, percentile, tool_name

INPUT_FIELDS = tuple(f for f in TOKEN_FIELDS if f != "output_tokens")
RETRY_SECONDS = 300


def context(request):
    return sum(request["usage"][f] for f in INPUT_FIELDS)


def aggregate(requests, prices):
    known = [r for r in requests if r["cost_usd_range"] is not None]
    components = {k: [0., 0.] for k in ("input", "output", "cache_read", "cache_write")}
    for r in known:
        rates, usage = prices["models"][r["model"]], r["usage"]
        base = 0.
        for key, field in (("input", "input_tokens"), ("output", "output_tokens"),
                           ("cache_read", "cache_read_input_tokens")):
            value = usage[field] * rates[key] / 1e6
            base += value
            for i in (0, 1):
                components[key][i] += value
        for i in (0, 1):
            components["cache_write"][i] += max(0., r["cost_usd_range"][i] - base)
    reasons = Counter()
    for r in requests:
        if r["cost_usd_range"] is None:
            reasons["price_book_missing" if not prices else "incomplete_usage" if not r["complete"]
                    else "nonstandard_pricing" if r["nonstandard"] else "unknown_model"] += 1
    return {"requests": len(requests), "tokens": {f: sum(r["usage"][f] for r in requests) for f in TOKEN_FIELDS},
            "priced_requests": len(known), "unpriced_requests": len(requests) - len(known),
            "unpriced_reasons": dict(reasons),
            "cost_usd_range": [sum(r["cost_usd_range"][i] for r in known) for i in (0, 1)] if known else None,
            "cost_components_usd_range": components if known else None}


def text_size(content):
    """Count observed UTF-8 text, not estimated tokens or hidden/image payloads."""
    if isinstance(content, str):
        return len(content.encode("utf-8", errors="replace")), 0
    if not isinstance(content, list):
        return 0, int(content is not None)
    size, other = 0, 0
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
            size += len(block["text"].encode("utf-8", errors="replace"))
        else:
            other += 1
    return size, other


class DeepAudit:
    def __init__(self, alias):
        self.alias = alias
        self.executions = {}
        self.owners = defaultdict(set)
        self.calls = {}
        self.results = {}
        self.compactions = []
        self.unidentified_results = 0

    def session(self, sid, raw, subagent, path):
        meta = self.executions.setdefault(sid, {"subagent": subagent, "candidates": set()})
        if subagent:
            meta["candidates"].add((self.alias("session", raw), "session_id"))
            if path.parent.name == "subagents":
                meta["candidates"].add((self.alias("session", path.parent.parent.name), "directory"))

    def tool(self, key, name, inputs, sid, timestamp, evidence):
        # Raw MCP names and arguments only feed the salted signature; never exported.
        signature = self.alias("arguments", json.dumps([name, inputs], sort_keys=True, ensure_ascii=True))
        tid = self.alias("tool", key)
        self.calls[tid] = {"id": tid, "session": sid, "name": tool_name(name),
                           "signature": signature, "timestamp": timestamp, "evidence": evidence}

    def result(self, block, sid, timestamp, evidence):
        key = block.get("tool_use_id")
        if not isinstance(key, str) or not key:
            self.unidentified_results += 1
            return
        tid = self.alias("tool", key)
        size, other = text_size(block.get("content"))
        flag = block.get("is_error")
        status = "error" if flag is True else "success" if flag is False or flag is None else "unknown"
        item = {"id": tid, "session": sid, "timestamp": timestamp, "evidence": evidence,
                "text_bytes": size, "nontext_blocks": other, "nontext_evidence": evidence, "status": status}
        old = self.results.get(tid)
        if old:
            if size > old["text_bytes"]:
                # Keep the size, observation time, and evidence from the same record.
                old["text_bytes"] = size
                old["timestamp"] = timestamp
                old["evidence"] = evidence
            if other > old["nontext_blocks"]:
                old["nontext_blocks"] = other
                old["nontext_evidence"] = evidence
            if old["status"] != status:
                old["status"] = "conflicting"
        else:
            self.results[tid] = item

    def build(self, report, prices, by_session):
        time_index = {sid: [parse_time(r["timestamp"]) for r in rows] for sid, rows in by_session.items()}
        time_counts = {sid: Counter(times) for sid, times in time_index.items()}
        def neighbors(sid, timestamp):
            rows, times = by_session.get(sid, []), time_index.get(sid, [])
            ts = parse_time(timestamp)
            left, right = bisect_left(times, ts), bisect_right(times, ts)
            before, after = rows[left - 1] if left else None, rows[right] if right < len(rows) else None
            ambiguous = left != right or any(time_counts[sid][parse_time(r["timestamp"])] > 1
                                             for r in (before, after) if r)
            # Tied timestamps do not establish event order.
            return (None, None, True) if ambiguous else (before, after, False)

        executions = []
        for session in report["sessions"]:
            sid, rows = session["id"], by_session.get(session["id"], [])
            trajectory = []
            for index, r in enumerate(rows):
                prev = rows[index - 1] if index else None
                tied = time_counts[sid][parse_time(r["timestamp"])] > 1 or (
                    prev is not None and time_counts[sid][parse_time(prev["timestamp"])] > 1)
                trajectory.append({"request": r["id"], "timestamp": r["timestamp"], "context_tokens": context(r),
                    "delta_tokens": context(r) - context(prev) if prev and not tied and r["complete"] and prev["complete"] else None,
                    "gap_seconds": (parse_time(r["timestamp"]) - parse_time(prev["timestamp"])).total_seconds() if prev and not tied else None,
                    "order_ambiguous": bool(tied), "complete": r["complete"],
                    "cache_write_tokens": r["usage"]["cache_creation_input_tokens"],
                    "cache_read_tokens": r["usage"]["cache_read_input_tokens"], "output_tokens": r["usage"]["output_tokens"],
                    "cost_usd_range": r["cost_usd_range"], "evidence": r["evidence"]})
            sizes = [context(r) for r in rows]
            meta = self.executions[sid]
            candidates = [(pid, basis) for pid, basis in meta["candidates"]
                          if pid in self.executions and not self.executions[pid]["subagent"]]
            parents = {pid for pid, _ in candidates}
            parent = next(iter(parents)) if len(parents) == 1 else None
            executions.append({"id": sid, "subagent": session["subagent"], "parent": parent,
                "parent_basis": sorted({basis for _, basis in candidates}) if parent else [],
                "parent_ambiguous": len(parents) > 1,
                **aggregate(rows, prices), "context_p50": percentile(sizes, .5), "context_p95": percentile(sizes, .95),
                "context_peak": max(sizes, default=0), "large_context_requests": sum(n >= 50000 for n in sizes),
                "trajectory": trajectory})

        families = defaultdict(list)
        for execution in executions:
            families[execution["parent"] or execution["id"]].append(execution["id"])
        groups = [{"id": root, "executions": members,
                   "unlinked_subagent": self.executions[root]["subagent"],
                   **aggregate([r for sid in members for r in by_session.get(sid, [])], prices)}
                  for root, members in families.items()]
        # Partial prices cannot be compared fairly; fall back to observed input volume.
        priced_all = bool(report["requests"]) and report["cost"]["unpriced_requests"] == 0
        def rank(item):
            return (-item["cost_usd_range"][1] if priced_all and item["cost_usd_range"] is not None
                    else -sum(item["tokens"][f] for f in INPUT_FIELDS), item["id"])
        groups.sort(key=rank)
        executions.sort(key=rank)

        matched = []
        unmatched = 0
        tool_totals = defaultdict(lambda: Counter())
        for tid, result in self.results.items():
            call = self.calls.get(tid)
            if not call or call["session"] != result["session"] or parse_time(result["timestamp"]) < parse_time(call["timestamp"]):
                unmatched += 1
                continue
            before, after, ambiguous = neighbors(call["session"], result["timestamp"])
            complete_pair = before and after and before["complete"] and after["complete"]
            matched.append({"tool": call["name"], "session": call["session"], "status": result["status"],
                "timestamp": result["timestamp"], "text_bytes": result["text_bytes"], "nontext_blocks": result["nontext_blocks"],
                "nontext_evidence": result["nontext_evidence"],
                "call_evidence": call["evidence"], "result_evidence": result["evidence"],
                "before_request": before["id"] if before else None, "after_request": after["id"] if after else None,
                "after_evidence": after["evidence"] if after else None,
                "neighbor_context_delta": context(after) - context(before) if complete_pair else None,
                "order_ambiguous": ambiguous})
            bucket = tool_totals[call["name"]]
            bucket["results"] += 1
            bucket["text_bytes"] += result["text_bytes"]
            bucket["errors"] += result["status"] == "error"
            bucket["nontext_blocks"] += result["nontext_blocks"]

        previous, retries, interval_requests = {}, [], {}
        def call_time_key(call):
            return call["session"], call["signature"], parse_time(call["timestamp"])
        call_counts = Counter(call_time_key(call) for call in self.calls.values())
        for call in sorted(self.calls.values(), key=lambda c: (parse_time(c["timestamp"]), c["id"])):
            key = (call["session"], call["signature"])
            prev = previous.get(key)
            previous[key] = call
            if not prev:
                continue
            if call_counts[call_time_key(call)] > 1 or call_counts[call_time_key(prev)] > 1:
                # Salted ID order cannot resolve simultaneous parallel calls.
                continue
            result = self.results.get(prev["id"])
            if not result or result["status"] != "error" or result["session"] != call["session"]:
                continue
            start, retry_at = parse_time(result["timestamp"]), parse_time(call["timestamp"])
            if not parse_time(prev["timestamp"]) <= start < retry_at or (retry_at - start).total_seconds() > RETRY_SECONDS:
                continue
            end_result = self.results.get(call["id"])
            if end_result and (end_result["session"] != call["session"] or parse_time(end_result["timestamp"]) < retry_at):
                end_result = None
            end = parse_time(end_result["timestamp"]) if end_result else retry_at
            rows, times = by_session.get(call["session"], []), time_index.get(call["session"], [])
            subset = rows[bisect_right(times, start):bisect_right(times, end)]
            interval_requests.update((r["id"], r) for r in subset)
            retries.append({"session": call["session"], "tool": call["name"],
                "error_at": result["timestamp"], "retry_at": call["timestamp"],
                "delay_seconds": (retry_at - start).total_seconds(),
                "outcome": end_result["status"] if end_result else "unobserved",
                "error_evidence": result["evidence"], "retry_evidence": call["evidence"],
                "result_evidence": end_result["evidence"] if end_result else None,
                "observed_interval": aggregate(subset, prices)})

        compactions = []
        for event in sorted(self.compactions, key=lambda e: (parse_time(e["timestamp"]), e["session"])):
            before, after, ambiguous = neighbors(event["session"], event["timestamp"])
            complete_pair = before and after and before["complete"] and after["complete"]
            compactions.append({**event, "before_request": before["id"] if before else None,
                "after_request": after["id"] if after else None, "before_evidence": before["evidence"] if before else None,
                "after_evidence": after["evidence"] if after else None,
                "context_before": context(before) if before and before["complete"] else None,
                "context_after": context(after) if after and after["complete"] else None,
                "context_delta": context(after) - context(before) if complete_pair else None,
                "next_cache_write_tokens": after["usage"]["cache_creation_input_tokens"] if after and after["complete"] else None,
                "order_ambiguous": ambiguous})
        detail = {"schema_version": 1, "ranking_basis": "api_equivalent_usd_upper" if priced_all else "input_tokens",
            "summary": aggregate(report["requests"], prices),
            "coverage": {"main_executions": sum(not e["subagent"] for e in executions),
                "subagent_executions": sum(e["subagent"] for e in executions),
                "linked_subagents": sum(e["parent"] is not None for e in executions),
                "unlinked_subagents": sum(e["subagent"] and e["parent"] is None for e in executions),
                "requests_seen_in_multiple_executions": sum(len(owners) > 1 for owners in self.owners.values()),
                "matched_tool_results": len(matched), "unmatched_tool_results": unmatched,
                "unidentified_tool_results": self.unidentified_results,
                "calls_without_result": sum(tid not in self.results for tid in self.calls),
                "ambiguous_tool_call_order": sum(call_counts[call_time_key(call)] > 1 for call in self.calls.values()),
                "conflicting_tool_results": sum(r["status"] == "conflicting" for r in self.results.values())},
            "groups": groups, "executions": executions,
            "tool_results_by_type": [{"tool": name, **dict(values)} for name, values in sorted(tool_totals.items())],
            "largest_tool_results": sorted(matched, key=lambda r: -r["text_bytes"])[:100],
            "retries": retries, "retry_window_seconds": RETRY_SECONDS,
            "retry_observed_union": aggregate(list(interval_requests.values()), prices),
            "compactions": compactions,
            "interpretation": [
                "長い文脈・大量のキャッシュ読み出しだけで無駄とは判定しません。入力は通常入力＋キャッシュ書込＋読出です。",
                "参考額は指定単価表のAPI換算。未換算があれば順位は入力トークン順。未知モデル・欠損・非標準料金を0円にしません。",
                "親子関係は保存先または共有sessionIdからの推定。親不明のサブエージェントは独立集計。各応答は最初に観測した実行へ一度だけ計上。",
                "再試行候補は同一実行・同じツール名と引数で、失敗結果後5分以内の再呼出。同時刻の同一引数呼出は前後関係を確定せず除外。引数を修正した再試行は対象外。",
                "再試行区間の使用量は記録時刻による観測値で、失敗が原因の費用・削減可能額ではありません。区間は重なるため合計せず、重複除外したunionを参照。",
                "ツール結果は保存されたテキストのUTF-8バイト数。トークン数・モデルが実際に受け取った量ではなく、画像・省略済み結果は含みません。",
                "結果前後・圧縮前後の文脈差は時刻上の隣接。並列処理等の影響があり因果関係は未確定。同時刻・欠損時は差を算出しません。",
                "全時系列はreport.json、画面は上位実行・候補のみ。原文の意味・成果物の品質は根拠行を別途確認します。"]}

        from .diagnosis import diagnose
        detail["diagnosis"] = diagnose(report, detail, prices, by_session, self.compactions,
                                        {rid for rid, owners in self.owners.items() if len(owners) > 1})
        return detail

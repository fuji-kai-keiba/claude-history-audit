"""Daily, weekly, and monthly usage tables in a chosen time zone. No IO."""
import re
from datetime import datetime, timedelta, timezone

from .audit import TOKEN_FIELDS, parse_time

UNITS = ("daily", "weekly", "monthly")
UNIT_LABELS = {"daily": "日", "weekly": "週（月曜始まり）", "monthly": "月"}


class Zone:
    """System local time by default. Fixed offsets and UTC need no tz database."""

    def __init__(self, spec="local"):
        spec = (spec or "local").strip()
        self.tz = None
        if spec.lower() == "local":
            offset = datetime.now().astimezone().strftime("%z")
            self.label = "local (" + offset[:3] + ":" + offset[3:] + ")"
            return
        if spec.upper() in ("UTC", "Z"):
            self.tz, self.label = timezone.utc, "UTC"
            return
        match = re.fullmatch(r"([+-])(\d{2}):?(\d{2})", spec)
        if match:
            minutes = int(match.group(2)) * 60 + int(match.group(3))
            if minutes > 14 * 60:
                raise ValueError("時差は -14:00 から +14:00 の範囲で指定してください。")
            sign = -1 if match.group(1) == "-" else 1
            self.tz = timezone(timedelta(minutes=sign * minutes))
            self.label = match.group(1) + match.group(2) + ":" + match.group(3)
            return
        try:
            from zoneinfo import ZoneInfo
            self.tz = ZoneInfo(spec)
        except Exception:
            raise ValueError("タイムゾーン " + spec + " を解決できません。local、UTC、+09:00 の形式を使ってください"
                             "（地域名は OS のタイムゾーンデータか tzdata が必要です）。")
        self.label = spec

    def convert(self, value):
        return value.astimezone(self.tz) if self.tz else value.astimezone()

    def midnight(self, day):
        """Start of a calendar date in this zone, as an aware datetime."""
        if self.tz:
            return datetime(day.year, day.month, day.day, tzinfo=self.tz)
        return datetime(day.year, day.month, day.day).astimezone()


def period_key(local, unit):
    if unit == "daily":
        return local.strftime("%Y-%m-%d")
    if unit == "weekly":
        return (local.date() - timedelta(days=local.weekday())).isoformat()
    return local.strftime("%Y-%m")


def _empty():
    return {"requests": 0, "tokens": {f: 0 for f in TOKEN_FIELDS}, "priced_requests": 0, "cost": [0.0, 0.0]}


def _add(bucket, request):
    bucket["requests"] += 1
    for field in TOKEN_FIELDS:
        bucket["tokens"][field] += request["usage"][field]
    cost = request.get("cost_usd_range")
    if cost is not None:
        bucket["priced_requests"] += 1
        bucket["cost"][0] += cost[0]
        bucket["cost"][1] += cost[1]


def _finish(bucket):
    return {"requests": bucket["requests"], "tokens": bucket["tokens"],
            "total_tokens": sum(bucket["tokens"].values()),
            "priced_requests": bucket["priced_requests"],
            "cost_usd_range": bucket["cost"] if bucket["priced_requests"] else None}


def period_rows(request_rows, zone, unit):
    """Aggregate already deduplicated request rows. Each row lists its models too."""
    if unit not in UNITS:
        raise ValueError("unit must be one of " + ", ".join(UNITS))
    buckets, models = {}, {}
    for request in request_rows:
        ts = parse_time(request["timestamp"])
        if ts is None:
            continue
        key = period_key(zone.convert(ts), unit)
        _add(buckets.setdefault(key, _empty()), request)
        _add(models.setdefault(key, {}).setdefault(request["model"], _empty()), request)
    rows = []
    for key in sorted(buckets):
        row = {"period": key}
        row.update(_finish(buckets[key]))
        row["models"] = [dict(model=name, **_finish(models[key][name])) for name in sorted(models[key])]
        rows.append(row)
    return rows


def all_periods(request_rows, zone):
    result = {"timezone": zone.label}
    for unit in UNITS:
        result[unit] = period_rows(request_rows, zone, unit)
    return result


def money(cost, priced, requests):
    if cost is None:
        return "未換算"
    text = f"${cost[0]:,.2f}" if abs(cost[1] - cost[0]) < 0.005 else f"${cost[0]:,.2f}–{cost[1]:,.2f}"
    return text if priced == requests else text + f"（{requests - priced}応答未換算）"


COLUMNS = [("期間", None), ("応答", "requests"), ("通常入力", "input_tokens"), ("キャッシュ書込", "cache_creation_input_tokens"),
           ("キャッシュ読出", "cache_read_input_tokens"), ("出力", "output_tokens"), ("合計", "total_tokens"), ("参考額", None)]


def _cells(label, row):
    values = [label]
    for _, key in COLUMNS[1:-1]:
        value = row[key] if key in ("requests", "total_tokens") else row["tokens"][key]
        values.append(f"{value:,}")
    values.append(money(row["cost_usd_range"], row["priced_requests"], row["requests"]))
    return values


def _width(text):
    return sum(2 if ord(ch) > 0x2E7F else 1 for ch in text)


def render_table(rows, unit, zone_label, breakdown=False):
    """Plain-text table for terminals. East Asian wide characters count as two columns."""
    lines_data = []
    total = _empty()
    for row in rows:
        lines_data.append(_cells(row["period"], row))
        if breakdown:
            for model in row["models"]:
                lines_data.append(_cells("  " + model["model"], model))
        total["requests"] += row["requests"]
        for field in TOKEN_FIELDS:
            total["tokens"][field] += row["tokens"][field]
        if row["cost_usd_range"] is not None:
            total["priced_requests"] += row["priced_requests"]
            total["cost"][0] += row["cost_usd_range"][0]
            total["cost"][1] += row["cost_usd_range"][1]
    header = [title for title, _ in COLUMNS]
    footer = _cells("合計", _finish(total))
    widths = [max(_width(r[i]) for r in [header, footer] + lines_data) for i in range(len(header))]

    def line(cells):
        out = []
        for i, cell in enumerate(cells):
            pad = " " * (widths[i] - _width(cell))
            out.append(cell + pad if i == 0 else pad + cell)
        return "  ".join(out).rstrip()

    rule = "-" * _width(line(header))
    body = [line(r) for r in lines_data] or ["（対象期間に応答がありません）"]
    return "\n".join([f"{UNIT_LABELS[unit]}ごとの利用（区切り: {zone_label}）", line(header), rule] + body + [rule, line(footer)])

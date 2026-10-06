import argparse
import json
import sys
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import __version__
from .audit import audit, iso, load_prices
from .periods import UNITS, Zone, render_table
from .report import cost_text, write_reports
from .collection import (collect, collection_notice, default_config, local_source,
                         read_config, sources_main, state_home)


def calendar_date(text):
    try:
        return datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        raise argparse.ArgumentTypeError("日付は YYYY-MM-DD（UTC）で指定してください。")


def instant(text):
    try:
        result = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError("timezone required")
        return result.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise argparse.ArgumentTypeError("時刻は 2026-09-06T00:00:00+09:00 のようにUTCオフセット付きで指定してください。")


def usage_main(argv):
    """Terminal tables like ccusage daily/weekly/monthly. Reads local history only; writes nothing."""
    parser = argparse.ArgumentParser(prog="audit.py usage",
        description="日別・週別・月別の利用量を表示。この環境の履歴（または --source）だけを読み、ファイルは書きません。")
    parser.add_argument("unit", nargs="?", choices=UNITS, default="daily", help="集計単位（既定 daily）")
    parser.add_argument("--source", action="append", type=Path, help="JSONLファイルまたはフォルダ。複数指定可")
    window = parser.add_mutually_exclusive_group()
    window.add_argument("--days", type=int, help="直近N日（既定30）")
    window.add_argument("--since", help="開始日 YYYY-MM-DD（--timezone の暦で、その日を含む）")
    window.add_argument("--all", action="store_true", help="残っている全期間")
    parser.add_argument("--until", help="終了日 YYYY-MM-DD（--timezone の暦で、その日を含む）")
    parser.add_argument("--timezone", default="local", help="日付の区切り。local（既定・OSの時刻設定）、UTC、+09:00、Asia/Tokyo など")
    prices_group = parser.add_mutually_exclusive_group()
    prices_group.add_argument("--price-book", type=Path, help="明示指定したAPI単価表で参考額を表示。請求額ではありません")
    prices_group.add_argument("--reference-prices", action="store_true", help="同梱の標準API単価表で参考額を表示。請求額ではありません")
    parser.add_argument("--breakdown", action="store_true", help="期間ごとにモデル別の行を表示")
    parser.add_argument("--json", action="store_true", help="表の代わりにJSONを出力")
    args = parser.parse_args(argv)
    if args.days is not None and args.days < 1:
        parser.error("--days は1以上にしてください。")
    try:
        zone = Zone(args.timezone)
        def day(text):
            try:
                return datetime.strptime(text, "%Y-%m-%d").date()
            except ValueError:
                raise ValueError("日付は YYYY-MM-DD で指定してください: " + text)
        now = datetime.now(timezone.utc)
        until = zone.midnight(day(args.until) + timedelta(days=1)) if args.until else now
        if args.all:
            since = None
        elif args.since:
            since = zone.midnight(day(args.since))
        else:
            first_day = zone.convert(until - timedelta(seconds=1)).date() - timedelta(days=(args.days or 30) - 1)
            since = zone.midnight(first_day)
        if since and since >= until:
            raise ValueError("開始日は終了日より前にしてください。")
        prices = load_prices(Path(__file__).with_name("reference_prices.json") if args.reference_prices else args.price_book)
        report, _ = audit(args.source or [local_source()], since=since, until=until, prices=prices, now=now, zone=zone)
    except (OSError, ValueError, TypeError, OverflowError) as error:
        print("集計できません: " + str(error), file=sys.stderr)
        return 2
    rows = report["periods"][args.unit]
    if args.json:
        print(json.dumps({"unit": args.unit, "timezone": report["periods"]["timezone"], "window": report["window"],
                          "cost_basis": report["cost"]["basis"], "rows": rows}, ensure_ascii=False, indent=2))
    else:
        print(render_table(rows, args.unit, report["periods"]["timezone"], args.breakdown))
        print("参考額はAPI単価による換算で、請求額ではありません。" if prices else "参考額は --reference-prices か --price-book を指定すると表示します。")
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "usage":
        return usage_main(argv[1:])
    if argv and argv[0] == "sources":
        return sources_main(argv[1:])
    if argv and argv[0] == "cloud":
        from .sync import main as sync_main
        return sync_main(argv[1:])
    parser = argparse.ArgumentParser(description="Claude Code履歴を監査。登録済みSSH先は自動取得。履歴変更・LLM呼び出しなし。")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--source", action="append", type=Path, help="JSONLファイルまたはフォルダ。複数指定可。既定: CLAUDE_CONFIG_DIR/projects または ~/.claude/projects")
    window = parser.add_mutually_exclusive_group()
    window.add_argument("--days", type=int, help="直近N日（既定30）。--until があればその日の終了から遡る")
    window.add_argument("--since", type=calendar_date, help="開始日を含む（UTC）")
    window.add_argument("--since-at", type=instant, help="開始時刻を含む。ISO 8601、UTCオフセット必須")
    window.add_argument("--all", action="store_true", help="残っている全期間を対象にする")
    end = parser.add_mutually_exclusive_group()
    end.add_argument("--until", type=calendar_date, help="終了日を含む（UTC）")
    end.add_argument("--until-at", type=instant, help="終了時刻は含まない。ISO 8601、UTCオフセット必須")
    parser.add_argument("--output", type=Path, help="新しい出力フォルダ。既定: ~/.claude-history-audit/reports/日時")
    price = parser.add_mutually_exclusive_group()
    price.add_argument("--price-book", type=Path, help="明示指定したAPI単価表で参考額を計算。請求額ではありません")
    price.add_argument("--reference-prices", action="store_true", help="同梱の標準API参考単価表を使用。実請求ではありません")
    parser.add_argument("--deep", action="store_true", help="文脈推移・親子実行・再試行・ツール結果量・圧縮前後を追加監査")
    parser.add_argument("--open", action="store_true", help="作成後、ローカルHTMLをブラウザで開く")
    parser.add_argument("--timezone", default="local", help="日別・週別・月別の区切り。local（既定）、UTC、+09:00 など。期間指定はUTCのまま")
    parser.add_argument("--sources-config", type=Path, help="取得先設定。省略時は ~/.claude-history-audit/sources.json があれば使用")
    parser.add_argument("--local-only", action="store_true", help="登録先に接続せず、この環境の既定の履歴だけを監査")
    parser.add_argument("--allow-partial", action="store_true", help="取得失敗時も取得できた範囲でレポート作成。終了コード3で一部取得を通知")
    args = parser.parse_args(argv)
    if args.days is not None and args.days < 1:
        parser.error("--days は1以上にしてください。")
    if (args.source and (args.sources_config or args.local_only)) or (args.local_only and args.sources_config):
        parser.error("--source、--sources-config、--local-only は併用できません。")
    now = datetime.now(timezone.utc)
    until = args.until + timedelta(days=1) if args.until else args.until_at or now
    since = None if args.all else args.since or args.since_at or until - timedelta(days=args.days if args.days is not None else 30)
    if since and since >= until:
        parser.error("開始日は終了日より前にしてください。")
    sources = args.source or [local_source()]
    output = args.output or Path.home() / ".claude-history-audit" / "reports" / now.strftime("%Y%m%dT%H%M%S%fZ")
    status, private = {"mode": "paths"}, {}
    try:
        dest = output.expanduser().resolve()
        if dest.exists() or dest.is_symlink():
            raise ValueError("出力先は新しいフォルダを指定してください。")
        prices = load_prices(Path(__file__).with_name("reference_prices.json") if args.reference_prices else args.price_book)
        zone = Zone(args.timezone)
        registry_path = (args.sources_config or default_config()).expanduser()
        if not args.source and not args.local_only and (args.sources_config or registry_path.exists()):
            config = read_config(registry_path)
            snapshot = state_home() / "collections" / now.strftime("%Y%m%dT%H%M%S%fZ")
            local_paths = ([local_source()] if config.get("include_local", True) else []) + [Path(s["path"]).expanduser() for s in config["sources"] if s["kind"] == "path"]
            for path in local_paths:
                root = path.resolve()
                for target in (snapshot.resolve(), output.expanduser().resolve()):
                    if target == root or root in target.parents:
                        raise ValueError("取得・出力先を履歴保存先の中には置けません。取得先のpathを確認してください。")
            print("登録済みの取得先を確認・収集しています…", flush=True)
            sources, status, private = collect(config, snapshot)
            print(collection_notice(status))
            print("取得記録（私的情報を含む）: " + str(snapshot / "collection.json"))
            if status["failed"] and not args.allow_partial:
                raise ValueError("取得が完了していない接続先があるため集計を中止しました。取得記録を確認してください。一部だけの集計は --allow-partial で明示できます。")
            if not sources:
                raise ValueError("登録した取得先に監査できるJSONL履歴がありません。")
        for source in sources:
            src = source.expanduser().resolve()
            if dest == src or src in dest.parents:
                raise ValueError("履歴の保存先の中へレポートは書き込みません。別の --output を指定してください。")
        report, local_map = audit(sources, since=since, until=until, prices=prices, now=now, deep=args.deep, zone=zone)
        report["collection"] = status
        local_map["collection_sources"] = private
        destination = write_reports(output, report, local_map)
    except (OSError, ValueError, TypeError, OverflowError) as error:
        print("監査できません: " + str(error), file=sys.stderr)
        return 2
    c = report["coverage"]
    print(collection_notice(status))
    print(f"監査完了: {c['sessions']}セッション / {c['unique_requests']}応答 / 改善候補{len(report['findings'])}件")
    print("参考額: " + cost_text(report) + "（請求額ではありません）")
    print("HTML: " + str(destination / "report.html"))
    print("要約: " + str(destination / "summary.md"))
    print("JSON: " + str(destination / "report.json"))
    print("local-map.json は実パスを含む私的ファイルです。")
    if args.open:
        webbrowser.open((destination / "report.html").as_uri())
    return 3 if status.get("failed") else 0


if __name__ == "__main__":
    raise SystemExit(main())

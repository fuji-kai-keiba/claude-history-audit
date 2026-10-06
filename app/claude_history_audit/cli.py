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
        description="日別・週別・月別の利用量を表示。この環境の保存履歴（または --source）だけを読み、ファイルは書かず登録済みSSH先にも接続しない。")
    parser.add_argument("unit", nargs="?", choices=UNITS, default="daily", help="集計単位（既定 daily）")
    parser.add_argument('--provider', choices=('claude', 'codex', 'auto'), help='既定保存先。省略時はClaude、codexはCodex、autoは両方。--sourceは自動判別')
    parser.add_argument("--source", action="append", type=Path, help="JSONLファイルまたはフォルダ。複数指定可")
    window = parser.add_mutually_exclusive_group()
    window.add_argument("--days", type=int, help="直近N日（既定30）")
    window.add_argument("--since", help="開始日 YYYY-MM-DD（--timezone の暦で、その日を含む）")
    window.add_argument("--all", action="store_true", help="残っている全期間")
    parser.add_argument("--until", help="終了日 YYYY-MM-DD（--timezone の暦で、その日を含む）")
    parser.add_argument("--timezone", default="local", help="日付の区切り。local（既定・OSの時刻設定）、UTC、+09:00、Asia/Tokyo など")
    price = parser.add_mutually_exclusive_group()
    price.add_argument("--price-book", type=Path, help="明示指定したAPI単価表で参考額を表示。請求額ではありません")
    price.add_argument("--reference-prices", action="store_true", help="同梱の標準API参考単価表（既定）")
    price.add_argument("--no-prices", action="store_true", help="参考額を出さず使用量だけ表示")
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
        prices = load_prices(None if args.no_prices else args.price_book or Path(__file__).with_name("reference_prices.json"))
        from .codex import local_sources as codex_sources
        defaults = codex_sources() if args.provider == 'codex' else ([local_source()] + codex_sources() if args.provider == 'auto' else [local_source()])
        report, _ = audit(args.source or defaults, since=since, until=until, prices=prices, now=now,
                          provider=args.provider or 'auto', zone=zone)
    except (OSError, ValueError, TypeError, OverflowError) as error:
        print("集計できません: " + str(error), file=sys.stderr)
        return 2
    rows = report["periods"][args.unit]
    if args.json:
        print(json.dumps({"unit": args.unit, "timezone": report["periods"]["timezone"], "window": report["window"],
                          "providers": report.get("providers"), "cost_basis": report["cost"]["basis"], "rows": rows},
                         ensure_ascii=False, indent=2))
    else:
        print(render_table(rows, args.unit, report["periods"]["timezone"], args.breakdown))
        print("参考額はAPI単価による換算で、請求額ではありません。" if prices else "参考額は表示していません（--no-prices）。")
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
    if argv and argv[0] == "review":
        from .evidence import main as review_main
        return review_main(argv[1:])
    parser = argparse.ArgumentParser(description="Claude Code / Codex履歴を監査。履歴変更・LLM呼び出しなし。")
    parser.add_argument('--provider', choices=('claude','codex','auto'), help='履歴形式と既定保存先。codexはCODEX_HOMEのsessions/archived_sessions。autoは両方。省略時の既定保存先はClaude、--sourceは自動判別')
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
    price.add_argument("--reference-prices", action="store_true", help="同梱の標準API参考単価表を使用（既定）。実請求ではありません")
    price.add_argument("--no-prices", action="store_true", help="API参考換算を行わず使用量だけ集計")
    detail = parser.add_mutually_exclusive_group()
    detail.add_argument("--deep", dest="deep", action="store_true", help="詳細監査を実行（既定・互換用）")
    detail.add_argument("--summary-only", dest="deep", action="store_false", help="明示的に詳細診断を省いた軽量集計")
    parser.set_defaults(deep=True)
    parser.add_argument("--open", action="store_true", help="作成後、ローカルHTMLをブラウザで開く")
    parser.add_argument("--timezone", default="local", help="期間別の表の日付区切り。local（既定）、UTC、+09:00 など。期間指定はUTCのまま")
    parser.add_argument("--sources-config", type=Path, help="取得先設定。省略時は ~/.claude-history-audit/sources.json があれば使用")
    parser.add_argument("--local-only", action="store_true", help="登録先に接続せず、この環境の既定の履歴だけを監査")
    parser.add_argument("--allow-partial", action="store_true", help="取得失敗時も取得できた範囲でレポート作成。終了コード3で一部取得を通知")
    args = parser.parse_args(argv)
    if args.days is not None and args.days < 1:
        parser.error("--days は1以上にしてください。")
    if (args.source and (args.sources_config or args.local_only)) or (args.local_only and args.sources_config):
        parser.error("--source、--sources-config、--local-only は併用できません。")
    if args.sources_config and args.provider in ('codex','auto'):
        parser.error('登録先の自動取得はClaude用です。Codexは --source で保存先を指定してください。')
    now = datetime.now(timezone.utc)
    until = args.until + timedelta(days=1) if args.until else args.until_at or now
    since = None if args.all else args.since or args.since_at or until - timedelta(days=args.days if args.days is not None else 30)
    if since and since >= until:
        parser.error("開始日は終了日より前にしてください。")
    from .codex import local_sources as codex_sources
    defaults = codex_sources() if args.provider == 'codex' else ([local_source()]+codex_sources() if args.provider == 'auto' else [local_source()])
    sources = args.source or defaults
    output = args.output or Path.home() / ".claude-history-audit" / "reports" / now.strftime("%Y%m%dT%H%M%S%fZ")
    status, private = {"mode": "paths"}, {}
    try:
        dest = output.expanduser().resolve()
        if dest.exists() or dest.is_symlink():
            raise ValueError("出力先は新しいフォルダを指定してください。")
        prices = load_prices(None if args.no_prices else args.price_book or Path(__file__).with_name("reference_prices.json"))
        zone = Zone(args.timezone)
        registry_path = (args.sources_config or default_config()).expanduser()
        if not args.source and not args.local_only and args.provider not in ('codex','auto') and (args.sources_config or registry_path.exists()):
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
        local_root = local_source().expanduser().resolve()
        config_dir = local_root.parent if any(Path(src).expanduser().resolve() == local_root for src in sources) else None
        report, local_map = audit(sources, since=since, until=until, prices=prices, now=now, deep=args.deep,
                                  provider=args.provider or 'auto', config_dir=config_dir, zone=zone)
        report["collection"] = status
        local_map["collection_sources"] = private
        destination = write_reports(output, report, local_map)
    except (OSError, ValueError, TypeError, OverflowError) as error:
        print("監査できません: " + str(error), file=sys.stderr)
        return 2
    c = report["coverage"]
    print(collection_notice(status))
    analysis = report.get('deep', {}).get('diagnosis', {}).get('analysis')
    print(f"集計済み: {c['sessions']}セッション / {c['unique_requests']}応答")
    if analysis:
        print(f"自動診断: {analysis['status']} / 具体的な改善候補 {len(analysis['findings'])}件 / 原文確認は未完了")
        print('同じ監査内で review-plan.json の全項目を確認し、review finalize で完了検査してください。')
    print("参考額: " + cost_text(report) + "（請求額ではありません）")
    print("HTML: " + str(destination / "report.html"))
    print("要約: " + str(destination / "summary.md"))
    print("JSON: " + str(destination / "report.json"))
    print("local-map.json は実パスを含む私的ファイルです。")
    if args.open:
        webbrowser.open((destination / "report.html").as_uri())
    if analysis and analysis['status'] != 'automatic_checks_passed':
        print('自動診断を完了できません。整合性または対象データを確認してください。', file=sys.stderr)
        return 4
    return 3 if status.get("failed") else 0


if __name__ == "__main__":
    raise SystemExit(main())

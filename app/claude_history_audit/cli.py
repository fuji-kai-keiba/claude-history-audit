import argparse
import json
import os
import sys
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import __version__
from .audit import audit, iso, load_prices
from .report import cost_text, write_reports


def calendar_date(text):
    try:
        return datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        raise argparse.ArgumentTypeError("日付は YYYY-MM-DD（UTC）で指定してください。")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Claude Codeの保存済み履歴をローカルで監査。通信・履歴変更・LLM呼び出しなし。")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--source", action="append", type=Path, help="JSONLファイルまたはフォルダ。複数指定可。既定: CLAUDE_CONFIG_DIR/projects または ~/.claude/projects")
    window = parser.add_mutually_exclusive_group()
    window.add_argument("--days", type=int, help="直近N日（既定30）。--until があればその日の終了から遡る")
    window.add_argument("--since", type=calendar_date, help="開始日を含む（UTC）")
    window.add_argument("--all", action="store_true", help="残っている全期間を対象にする")
    parser.add_argument("--until", type=calendar_date, help="終了日を含む（UTC）")
    parser.add_argument("--output", type=Path, help="新しい出力フォルダ。既定: ~/.claude-history-audit/reports/日時")
    parser.add_argument("--price-book", type=Path, help="明示指定したAPI単価表で参考額を計算。請求額ではありません")
    parser.add_argument("--open", action="store_true", help="作成後、ローカルHTMLをブラウザで開く")
    args = parser.parse_args(argv)
    if args.days is not None and args.days < 1:
        parser.error("--days は1以上にしてください。")
    now = datetime.now(timezone.utc)
    until = args.until + timedelta(days=1) if args.until else now
    since = None if args.all else args.since or until - timedelta(days=args.days if args.days is not None else 30)
    if since and since >= until:
        parser.error("開始日は終了日より前にしてください。")
    config = Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))).expanduser()
    sources = args.source or [config / "projects"]
    output = args.output or Path.home() / ".claude-history-audit" / "reports" / now.strftime("%Y%m%dT%H%M%S%fZ")
    try:
        dest = output.expanduser().resolve()
        for source in sources:
            src = source.expanduser().resolve()
            if dest == src or src in dest.parents:
                raise ValueError("履歴の保存先の中へレポートは書き込みません。別の --output を指定してください。")
        prices = load_prices(args.price_book)
        report, local_map = audit(sources, since=since, until=until, prices=prices, now=now)
        destination = write_reports(output, report, local_map)
    except (OSError, ValueError, TypeError, OverflowError) as error:
        print("監査できません: " + str(error), file=sys.stderr)
        return 2
    c = report["coverage"]
    print(f"監査完了: {c['sessions']}セッション / {c['unique_requests']}応答 / 改善候補{len(report['findings'])}件")
    print("参考額: " + cost_text(report) + "（請求額ではありません）")
    print("HTML: " + str(destination / "report.html"))
    print("要約: " + str(destination / "summary.md"))
    print("JSON: " + str(destination / "report.json"))
    print("local-map.json は実パスを含む私的ファイルです。")
    if args.open:
        webbrowser.open((destination / "report.html").as_uri())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

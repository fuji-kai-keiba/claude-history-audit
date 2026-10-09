#!/usr/bin/env python3
"""Install a standalone copy; never overwrite an existing skill implicitly."""
import argparse
import os
import shutil
import tempfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="個人用Claude Codeへ /claude-history-audit をインストール")
    parser.add_argument("--destination", type=Path, help="新しいスキルフォルダ。既定: CLAUDE_CONFIG_DIR/skills/claude-history-audit")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    config = Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))).expanduser()
    destination = (args.destination or config / "skills" / "claude-history-audit").expanduser().absolute()
    if destination.exists() or destination.is_symlink():
        parser.error("既存スキルは上書きしません。別の --destination を指定するか既存版を退避してください。")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".claude-audit-install-", dir=str(destination.parent)) as temp:
        staging = Path(temp) / "skill"
        shutil.copytree(root / "skills" / "claude-history-audit", staging)
        shutil.copyfile(root / "LICENSE", staging / "LICENSE")
        scripts = staging / "scripts"
        scripts.mkdir()
        shutil.copytree(root / "app" / "claude_history_audit", scripts / "claude_history_audit",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (scripts / "run.py").write_text("#!/usr/bin/env python3\nfrom claude_history_audit.cli import main\nraise SystemExit(main())\n", encoding="utf-8")
        os.rename(staging, destination)
    print("インストール完了: " + str(destination))
    print("Claude Codeで /claude-history-audit を実行してください。表示されなければセッションを再起動してください。")


if __name__ == "__main__":
    main()

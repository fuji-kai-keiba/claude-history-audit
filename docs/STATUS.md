# Status

2026-10-05: Web画面へ数値のみを同期する端末ツールを追加。最終検証・独立レビューは末尾のHarness欄を参照。

- ローカル集計・SSH/共有フォルダ取得は既存機能として維持。
- cloud sync/status、独立Python zipアプリ、HTTPS限定転送、許可リスト、安定HMAC ID、分割同期、成功確認に対応。
- Windowsは現在のユーザーがログイン中に15分ごとのタスクを登録。パスは引用し、接続鍵をコマンドラインに含めない。Mac/Linux/WSLは手動sync。
- 全50件のunittest、独立スキル導入・CLIレポート・配布zipアプリのスモークテスト成功。配布pyzを独立プロセスでinstallし、実HTTPSで501件を2分割送信、認証ヘッダー、本文除外、受信完了と状態保存まで検証（合成履歴・一時CAを使用、TLS検証は有効）。Windowsタスク登録はモックでコマンドと失敗処理を検証。
- 実Windows端末は未接続。本人のログイン、実PCへのインストールと定期実行は未検証。Web側の複数利用者・端末の結合検証は別案件で実施。
- 元履歴・監査結果・接続設定・接続鍵はGitへ保存しない。
- GitHub: https://github.com/fuji-kai-keiba/claude-history-audit （private）
- Web: https://github.com/fuji-kai-keiba/claude-history-audit-web （別案件、非公開サイト）

## 起動

ローカル監査: `python3 scripts/audit.py --days 30 --open`

SSH登録: `python3 scripts/audit.py sources add-ssh work user@server`

スキル: `python3 scripts/install_skill.py` → Claude Codeで `/claude-history-audit`

Web同期: Webから各PC専用ZIPを取得し、Windowsは `install-windows.cmd`。その他は `python3 audit-agent.pyz install --config device.json`、更新は `sync`。

## 検証

`python3 -m unittest discover -s tests -v`

`python3 scripts/smoke_test.py`

## 制約・次の作業

未登録端末、削除済み履歴、公式組織APIコネクター、請求との自動突合は対象外。Webに受信済みの使用量は端末側の履歴削除後も保持。現版は同期ごとに端末の保存履歴全体を処理する。大規模利用での負荷測定・差分同期は今後の拡張。

本人が各PCで一度設定し、実Windowsで最終同期と定期更新を確認する。WSLとWindowsの履歴保存先は別。SSHの実接続とClaudeモデルによるスキル呼び出しも未実施。

<!-- harness:start -->
## Harness

- Task: 9e1a6e053c5a / Web監査用の数値同期とWindows自動実行
- 状態: done
- 次の作業: 完了。変更が生じた場合は再検証する
- 試行: 0/3
- 記録: .harness/9e1a6e053c5a/task.json
<!-- harness:end -->

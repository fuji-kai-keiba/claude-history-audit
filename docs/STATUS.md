# Status

2026-10-05: 登録した別PC・サーバーからの自動取得を追加。最終検証・レビュー状態は末尾のHarness欄を参照。

- SSH先/共有フォルダの登録、監査時の自動収集、既存集計との統合を実装。
- Pythonの読み取り専用収集プログラムをSSH経由で実行。転送・時間上限、アーカイブ検証、私的なスナップショット保存に対応。
- 取得失敗は通常停止。一部集計は明示指定と終了コード3、取得範囲はHTML/Markdown/JSONに表示する。
- 全38件の回帰テストと独立スキル導入E2Eを実行済み。追加の事前検証ケースを含めた最終件数はHarnessの証拠に記録する。
- SSH送信プログラムと受信処理は合成データ・実Pythonプロセスで結合検証。実際の別端末へのSSH接続は未実施（接続先未提供、このMacにSSHのHost設定なし）。
- スキルを更新し、EnterpriseのCompliance APIによる公式取得とPro/Maxの各環境からの取得を区別して説明。公式APIコネクターは未実装。
- 元履歴・監査結果・接続先はGitに保存しない。
- GitHub: https://github.com/fuji-kai-keiba/claude-history-audit （private）

## 起動

`python3 scripts/audit.py sources add-ssh work user@server`

`python3 scripts/audit.py --days 30 --open`

取得先未登録の場合はこの端末の保存履歴のみ。登録済みなら監査のたびに収集する。バックグラウンドの定期実行は設定しない。

スキル: `python3 scripts/install_skill.py` → Claude Codeで `/claude-history-audit`

## 検証

`python3 -m unittest discover -s tests -v`

`python3 scripts/smoke_test.py`

## 既知の制約

未登録端末、削除済み履歴、公式アカウントAPI、請求との自動突合は対象外。相手側Python3と非対話SSH接続が必要。ホスト鍵は通常のSSHで確認済みのものを使う。元履歴コピーは自動削除しない。

## 次の作業

実際に利用している別PC・サーバーのSSH接続先または共有履歴パスを登録し、実接続で取得範囲を確認する。Claudeモデルによるスラッシュコマンド呼び出しは未実施。

<!-- harness:start -->
## Harness

- Task: bb64873d3bc9 / 複数端末の履歴を自動取得して監査
- 状態: done
- 次の作業: 完了。変更が生じた場合は再検証する
- 試行: 0/3
- 記録: .harness/bb64873d3bc9/task.json
<!-- harness:end -->

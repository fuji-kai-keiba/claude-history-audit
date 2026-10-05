# Claude History Audit

Claude Codeの**保存済み履歴から、費用・手戻りの原因候補を調べる**監査ツールです。登録したPC・サーバーからの自動取得に対応します。

会話を外部AIへ送らず、Python標準ライブラリだけでJSONLを集計します。ブラウザで見られる日本語HTML、機械処理用JSON、短いMarkdown要約を出力します。Claude Code用の `/claude-history-audit` スキルも同梱しています。

## 最短で使う

Python 3.9以降。追加パッケージ・APIキーは不要です。

```bash
git clone https://github.com/fuji-kai-keiba/claude-history-audit.git
cd claude-history-audit
python3 scripts/audit.py --days 30 --open
```

取得先を未登録の場合は `CLAUDE_CONFIG_DIR/projects`、未設定なら `~/.claude/projects` の保存履歴だけを読みます。**同じアカウントでも、この実行環境だけの履歴がアカウント全体を表すわけではありません。** 別端末のコピーや独自の保存先にも対応します。

```bash
python3 scripts/audit.py --source /path/to/transcripts --all
python3 scripts/audit.py --since 2026-10-01 --until 2026-10-05
python3 scripts/audit.py --source /path/to/pc-a --source /path/to/pc-b --days 30
```

日付はUTC。`--until` は指定日を含みます。履歴が削除済みの場合は復元しません。標準の保持期間は30日なので、調査対象を早めに保全してください。

## 別のPC・サーバーから自動取得

一度取得先を登録すると、通常の監査コマンドやスキルの実行時に毎回取得して統合集計します。アカウントへのログインだけで端末を発見する機能ではありません。

```bash
python3 scripts/audit.py sources add-ssh work user@server
python3 scripts/audit.py --days 30 --open
```

SSHのHost名でも登録できます。接続先はMac/Linux/WSLなどの `python3` が使える環境で、相手へのツールのインストールは不要です。SSH鍵認証と確認済みのホスト鍵が必要です。独自ポート・踏み台・鍵ファイルは通常のSSH設定を利用します。未知のホスト鍵を自動承認せず、パスワード入力待ちにもなりません。

既定では相手側の `CLAUDE_CONFIG_DIR/projects` または `~/.claude/projects` を読みます。非対話SSHへ環境変数が引き継がれない場合や、保存先が異なる場合は明示してください。

```bash
python3 scripts/audit.py sources add-ssh other other-server --path /path/to/claude/projects
python3 scripts/audit.py sources add-path shared /mounted/other-pc/claude/projects
python3 scripts/audit.py sources list
python3 scripts/audit.py sources remove other
python3 scripts/audit.py sources local off
```

設定は `~/.claude-history-audit/sources.json`。`sources local off` はこの端末の標準履歴を外し、`on` で戻します。`--sources-config /path/to/config.json` で別設定も指定可能です。手動の `--source` または `--local-only` を使うとSSH取得は行いません。

**取得失敗がある場合は既定で集計を止めます。** 一部だけのレポートが必要なら `--allow-partial` を指定します。この場合もレポートに取得漏れを明示し、終了コード3を返します。全件取得は0、取得・集計エラーは2です。接続先の履歴が空だった場合も「履歴なし」と表示します。過去の取得成功分をこっそり再利用しません。

SSH取得した元JSONLと私的な取得記録は `~/.claude-history-audit/collections/<日時>/` に保存します。元データに書き込まず、認証情報ファイルは取得しません。自動削除は行わないため、不要な取得コピーはこのフォルダから削除できます。既定は1取得先につき180秒・元履歴1GiBまで。設定の各取得先に `timeout_seconds` と `max_bytes` を指定すると変更できます。定期スケジュールの登録は行わず、**監査を実行するたびの自動取得**です。

## 契約と公式APIの違い

| 対象 | 取得方法 |
|---|---|
| 個人Pro/Maxの詳細な履歴 | このツールで各実行環境の保存履歴を取得・集計 |
| Enterpriseの対象セッション履歴 | 有効化されたCompliance APIと専用権限で公式取得が可能 |
| 組織の利用量・費用・活動指標 | 契約に対応するAnalytics API・管理画面で確認 |

Enterpriseも履歴が端末にしかない、という説明は正しくありません。[公式のセッション取得API](https://platform.claude.com/docs/en/manage-claude/compliance-sessions)には保存期間・認証方法・ZDR等による対象外があります。[Analytics APIの選び方](https://platform.claude.com/docs/en/manage-claude/analytics-api)も参照してください。**現版のツールはSSH/パス収集に対応し、これらの公式APIコネクターは未実装です。** 個人Pro/Maxのログイン情報から組織APIを呼び出すことはしません。

## Claude Codeから使う

```bash
python3 scripts/install_skill.py
```

個人用の `~/.claude/skills/claude-history-audit/`（または `CLAUDE_CONFIG_DIR` 以下）へ、実行コードを含む独立したコピーをインストールします。既存スキルは上書きしません。チェックアウトを移動しても利用できます。

Claude Codeで:

```text
/claude-history-audit
```

「先月分を監査」「この履歴フォルダを監査」のように対象を指定できます。表示されなければClaude Codeのセッションを再起動してください。更新時は既存スキルを退避して再インストールします。

**監査CLIはモデル呼び出しなし。登録済みSSH先があれば取得のために通信します。Claude Codeに結果を解釈させると、その解釈には通常のClaude利用が発生します。** スキルはまず集計レポートを読み、元履歴全体をモデルに投入しません。

## 出力

既定: `~/.claude-history-audit/reports/<UTC日時>/`

| ファイル | 内容 |
|---|---|
| `report.html` | オフラインの日本語ダッシュボード。セッションを絞り込み可能 |
| `report.json` | 集計・応答単位の使用量・改善候補・根拠ID |
| `summary.md` | Claude Codeや人間が読む短い要約 |
| `local-map.json` | 根拠IDと実パス・元セッションIDの対応表。私的情報を含む |

本文・ユーザー名・案件名・コマンド本文・ファイルパスは集計レポートに出しません。IDは監査ごとの秘密値で仮名化します。日時や利用統計まで匿名性を保証するものではありません。`local-map.json` は共有対象から外してください。

出力ファイルは所有者のみ読み書き可能な権限で作成します。既存フォルダを上書きせず、履歴保存先の中には出力しません。監査結果と元ログをGitへ追加しないでください。

## 何が分かるか

- モデル・セッション別の入力、出力、キャッシュ書き込み・読み出し
- 同じ応答が複数行・複数ファイルにある場合の重複除外
- 入力コンテキストのP50/P95、キャッシュ読み出しのトークン比率
- 同じ範囲のReadの反復、明示的なツール失敗、圧縮境界
- 営業提案・調査・会議・開発などのキーワードによる参考分類
- 対象期間・件数、欠損・不正レコード・未知の使用量のカバレッジ

改善候補には「観測」「解釈」「次の検証」と根拠ファイルID・行番号を付けます。再読込や大きい入力が必要な作業もあるため、自動的に無駄とは判定しません。

## 金額の扱い

既定はトークン集計だけです。明示指定した単価表でのみAPI換算を行います。

```bash
python3 scripts/audit.py --days 30 --price-book docs/prices.example.json
```

例の単価表は2026-10-05確認の一部モデルの標準API単価です。使用前に公式料金・契約と照合してください。モデルIDは完全一致で扱い、不明モデルやfastモードなどは推測せず未換算にします。キャッシュTTLの内訳がない場合は5分/1時間の単価による範囲を出します。

**参考額は請求額ではありません。** 固定席代、契約割引、税、外部ツール費用、未登録端末・Web・Coworkの利用を自動で含めません。請求CSVの取り込み・全社請求との自動突合は現版の対象外です。

## 検証

```bash
python3 -m unittest discover -s tests -v
python3 scripts/smoke_test.py
```

合成データだけを使います。内部JSONL形式は変更されるため、新しいバージョンに対応するときは実ログを公開せず、匿名の合成回帰ケースを追加してください。

詳しい集計ルールは [docs/METHODOLOGY.md](docs/METHODOLOGY.md)、開発状態は [docs/STATUS.md](docs/STATUS.md) を参照してください。

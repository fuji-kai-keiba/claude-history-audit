# Claude History Audit

Claude Codeの**保存済み履歴から、費用・手戻りの原因候補を調べる**ローカル監査ツールです。

会話を外部AIへ送らず、Python標準ライブラリだけでJSONLを集計します。ブラウザで見られる日本語HTML、機械処理用JSON、短いMarkdown要約を出力します。Claude Code用の `/claude-history-audit` スキルも同梱しています。

## 最短で使う

Python 3.9以降。追加パッケージ・APIキーは不要です。

```bash
git clone https://github.com/fuji-kai-keiba/claude-history-audit.git
cd claude-history-audit
python3 scripts/audit.py --days 30 --open
```

既定では `CLAUDE_CONFIG_DIR/projects`、未設定なら `~/.claude/projects` の保存履歴を読みます。別端末のコピーや独自の保存先にも対応します。

```bash
python3 scripts/audit.py --source /path/to/transcripts --all
python3 scripts/audit.py --since 2026-10-01 --until 2026-10-05
python3 scripts/audit.py --source /path/to/pc-a --source /path/to/pc-b --days 30
```

日付はUTC。`--until` は指定日を含みます。履歴が削除済みの場合は復元しません。標準の保持期間は30日なので、調査対象を早めに保全してください。

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

**監査CLIはモデル呼び出しなし。Claude Codeに結果を解釈させると、その解釈には通常のClaude利用が発生します。** スキルはまず集計レポートを読み、元履歴全体をモデルに投入しません。

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

**参考額は請求額ではありません。** 固定席代、契約割引、税、外部ツール費用、他端末・Web・Coworkの利用を自動で含めません。請求CSVの取り込み・全社請求との自動突合は現版の対象外です。

## 検証

```bash
python3 -m unittest discover -s tests -v
python3 scripts/smoke_test.py
```

合成データだけを使います。内部JSONL形式は変更されるため、新しいバージョンに対応するときは実ログを公開せず、匿名の合成回帰ケースを追加してください。

詳しい集計ルールは [docs/METHODOLOGY.md](docs/METHODOLOGY.md)、開発状態は [docs/STATUS.md](docs/STATUS.md) を参照してください。

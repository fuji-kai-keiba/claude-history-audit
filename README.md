# Claude History Audit

Claude Code・Codexの**利用者自身の保存履歴を基準に、問題候補の探索・原文確認・具体的な改善策まで一回の監査で行う**ツールです。特定の利用者の通常値を他の人へ当てはめず、Claudeは登録したPC・サーバーからの自動取得にも対応します。

会話を外部AIへ送らず、Python標準ライブラリだけでJSONLを集計します。ブラウザで見られる日本語HTML、機械処理用JSON、短いMarkdown要約を出力します。Claude Code用の `/claude-history-audit` スキルも同梱しています。

## 最短で使う

Python 3.9以降。追加パッケージ・APIキーは不要です。

**Claude Codeでこのリポジトリを開き「このPCの直近30日を監査して、改善策まで出して」と依頼できます。** `CLAUDE.md` が一連の手順へ案内するため、リポジトリ内で使う場合はスキルの別途インストールも不要です。Claudeが原文確認と完了検査まで進めます。

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

日付はUTC。`--until` は指定日を含みます。履歴が削除済みの場合は復元しません。Claude Codeの保持設定などで履歴が消えるため、調査対象を早めに保全してください。

## Codexを監査する

```bash
python3 scripts/audit.py --provider codex --days 30 --open
python3 scripts/audit.py --provider auto --days 30 --open
```

Codexは `CODEX_HOME`（未設定なら `~/.codex`）の `sessions` と `archived_sessions` を自動で探します。`auto` はこのPCのClaudeとCodexを集計します。コピー済みの履歴は `--source /path/to/history` で指定できます。Windowsは `py -3`。アカウントへログインして他端末の履歴を取得する機能ではありません。Codex/autoではClaudeのSSH取得設定を使わず、既存Web同期はClaudeのみです。

本人内比較、モデル別・親子別の参考額、文脈増加、圧縮、待機・進捗確認の反復を標準で調べ、根拠行と比較する改善案を出します。Codexでこのリポジトリを開き「Codexの直近30日を監査して」と依頼すれば、`AGENTS.md` が原文確認と完了検査まで案内します。CLI単体の自動診断は内容の必要性・品質を確認済みとは扱いません。

応答別usageを優先し、累計通知の重複加算を避けます。旧形式は差分と最終応答が一致した記録に限定し、欠損・曖昧な記録をカバレッジへ残します。画像を長文と数えず、推論トークンを出力へ再加算しません。待機候補の費用は確定削減額ではありません。ClaudeのTTLはCodexに適用しません。

旧形式で親の履歴が混在する子スレッドは、使用量の所有者を特定できないため換算・集計から除外し、除外件数を表示します。子自身の使用量が残らない場合もあり、完全取得とは扱いません。

Codex参考額は[OpenAI標準API単価](https://developers.openai.com/api/docs/pricing)による仮定の換算です（2026-10-06確認）。GPT-6 Astra / GPT-6.1 Sol / GPT-6 Lunaの標準・長文単価を同梱。速度未記録は標準速度と仮定し、未知モデル・非標準速度・キャッシュ内訳欠損は未換算。ChatGPT/Codex契約の請求額やクレジット消費量とは一致しません。

## 利用者ごとに問題候補を探す

最初に、対象範囲のモデル・親/子・用途の参考分類ごとの利用分布を作ります。入力・出力・通常入力・書込のいつもとの違い、同じ実行の途中からの増加、同系統の作業への利用集中を調べます。出力の比較には入力規模の区分も使います。中央値とばらつきから探索用の境界を計算するため、少量利用で出力が50から2,000 tokensへ増えたケースも拾えます。一方、毎回90万tokensの資料を必要として扱う人を、絶対量だけで異常と判定しません。

比較だけでは普段から一様に存在する問題を発見できないため、既知のルールに該当しない作業も、利用の集中と通常の代表例から原文確認の対象に選びます。Claudeは用途・必要な処理・既存ルール以外の問題・不足情報を確認し、新しい発見を根拠付きで記録できます。休憩やキャッシュなどの固定ルールは原因を調べる補助情報です。

**同じ人の指定した履歴範囲という前提で比較します。** 所有者の認証・複数利用者の自動分離はしません。複数人分が混ざる場合は利用者ごとに分けて実行してください。少数データは比較不足として残し、用途の参考分類が不正確な可能性も明示します。「候補なし」を「問題なし」と扱いません。全利用者での診断精度を実証した製品ではなく、履歴に残らない問題や成果物品質を保証するものでもありません。

## 初回から原因候補まで調べる

```bash
python3 scripts/audit.py --days 30 --open
```

通常実行で詳細監査と同梱の標準API参考換算まで出します。追加の「深掘り」指定は不要です。`--deep --reference-prices` も互換用に残しています。

Windowsでは `python3` の代わりに `py -3` も使えます。既存のチェックアウトは `git pull --ff-only` で更新してください。

- 親会話とサブエージェントの内訳、紐付けた作業全体の使用量・参考額。
- 上位実行の文脈推移、急増した応答、キャッシュ書込・読出・出力別の参考額。
- 大きなツール結果（保存テキストbytes）と前後の文脈差。
- 失敗後5分以内に同じツール・同じ引数で再実行した箇所と結果。
- 圧縮前後の文脈・直後の書込、原文を確認するための行番号。
- 書込の全量分類（初回観測・モデル変更・圧縮・間隔・重なり・不明・未分類）と未換算/TTL不明の規模。
- 観測・未確認の仮説・根拠行・確認方法・比較実験をまとめた初回診断。
- 休憩時間別の書込中央値・全文再書込率・参考額。同じ実行/モデルでの通常応答との比較と、支持されなかった仮説。
- 条件に応じた変更案、代替説明、対象の観測額による順位、変更の手間、品質と総費用の比較方法。
- 本人内の差・利用の集中・通常の代表例・既知のルールから選んだ根拠確認計画。項目漏れや根拠の変更を検出する完了検査。

たとえば「同じ実行・モデルで、通常応答では全文再書込0%、1時間超の再開では100%、書込はほぼ1時間TTL」という記録なら、初回から休憩前の引継メモと再開時の新しい会話を比較する案を出します。モデル変更や圧縮が重なる応答はこの比較から除外します。これは**失効との関連を示す診断で、失効の直接証明や確定削減額ではありません**。比較対象が不足・条件が異なる・TTL不明なら支持された仮説にせず、理由を残します。

画面は上位候補を表示し、全応答の時系列と全再試行・圧縮境界は `report.json` の `deep` に保存します。通常集計の合計値は変えません。費用が一部でも未換算なら入力トークン順に並べ、費用が安いと誤認させないようにします。

**再試行中に観測した費用や文脈の増加は、失敗が原因の費用・削減可能額とは限りません。** 資料の品質や内容の重複は、上位候補の根拠行と成果物を別途確認します。詳細診断はローカルレポート用で、現在のWeb同期には送りません。

ccusage等との突き合わせでは、同じPC・保存先・開始時刻・終了時刻に揃えます。次は日本時間9月6日から10月6日直前までの例です。

```bash
python3 scripts/audit.py --deep --reference-prices --since-at 2026-09-06T00:00:00+09:00 --until-at 2026-10-06T00:00:00+09:00
```

`--since-at` は開始を含み、`--until-at` は終了を含みません。時差を必ず指定します。日付版の `--until`（指定日を含む）とは意味が違います。重複除外やセッション定義も比較してください。

## なぜトークンがかかるかを分解する

通常実行（詳細監査）で、Claude の要求を実行ごとに並べ直し、費用の仕組みを分解した節「なぜトークンがかかるか」を出します（`report.json` の `anatomy`）。

- 要求ごとに文脈全体が再送されるため、各トークンの費用は「大きさ × 文脈に残った間の要求回数」で決まる。要求間の文脈増分を、自分の過去の出力（思考を含む）・起動時の固定分・ツール結果（ツール別）・ユーザー発言/system・画像へ配分し、キャッシュ読出を発生源別に示す。
- 出力が次の文脈に残る割合（持ち越し係数）、思考の割合の推定、人の発言1回あたりの要求数、読み直し倍率、ツールを1個ずつ呼んだ要求の数、起動時文脈の中央値。
- この端末の現在の設定のうち費用に効く許可リストの値（サブエージェントの既定モデル、effort、自動圧縮、キャッシュ期間、出力上限、フック数・プラグイン数）と、起動時に読む指示ファイル（CLAUDE.md・@取込・記憶の索引）の大きさ。値以外・秘密・フックのコマンド・本文・パスは集計に入れない（パスは local-map.json のみ）。設定は `--source` で別保存先を指定した場合は読まない。
- キャッシュ期間を5分にした場合の試算と、仕組みごとの判定（構造で解消不可／設定で調整可／使い方・ルールで調整可／現状維持が有利）。

配分は回帰による推定です。決定係数・半分割の安定性・配分合計と実読出の照合を併記します。

## 別のPC・サーバーから自動取得

Web画面への自動同期を使いたい場合は、後述の「どのPCからもWebで確認する」を使ってください。この節は監査実行時に別端末から取得する方式です。

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

Enterpriseも履歴が端末にしかない、という説明は正しくありません。[公式のセッション取得API](https://platform.claude.com/docs/en/manage-claude/compliance-sessions)には保存期間・認証方法・ZDR等による対象外があります。[Analytics APIの選び方](https://platform.claude.com/docs/en/manage-claude/analytics-api)も参照してください。**現版はSSH/パス収集と端末からの数値同期に対応し、これらの公式APIコネクターは未実装です。** 個人Pro/Maxのログイン情報から組織APIを呼び出すことはしません。

## どのPCからもWebで確認する

別リポジトリ [claude-history-audit-web](https://github.com/fuji-kai-keiba/claude-history-audit-web) の非公開Web画面を利用します。WebへのログインはChatGPTアカウントです。Claudeのログインや契約とは別で、Claudeの認証情報は取得しません。

1. 利用しているPCでWeb画面にログインし、「PCを追加」から設定ZIPをダウンロードする。
2. ZIPを展開する。WindowsはPython 3.9以降を用意し、`install-windows.cmd` を実行する。
3. 初回同期が終わると、Windowsにログイン中は15分ごとに更新する。各PCで一度設定すれば、どのPCのブラウザからも統合結果を確認できる。

同期先はダウンロード設定のHTTPSサイトに限定します。送るのは日時・モデル・トークン数・監査件数とHMACで仮名化したIDです。会話本文・パス・元ID・ローカル証拠マップ・Claudeの認証情報は送信しません。通常のオフラインレポートは従来どおり実行ごとのIDですが、Web同期は同じ利用者のPC間で安定したIDを使い、コピーした履歴の二重計上を防ぎます。

未登録PCや削除済みの履歴をアカウントから復元するものではありません。WindowsとWSLは履歴の保存環境が異なります。WSLで使っている場合はWSL内で下記を実行します。Mac/Linux/WSLの定期実行は現版では自動登録しません。

```bash
python3 audit-agent.pyz install --config device.json
python3 audit-agent.pyz sync --config device.json
python3 audit-agent.pyz status --config device.json
```

独自の履歴保存先は導入時に `--source /path/to/projects`（複数可）を指定すると保存します。通常はそのユーザーの `CLAUDE_CONFIG_DIR/projects` または `~/.claude/projects`。SSHの取得先設定はWeb同期に適用しません。

導入先はWindowsの `%LOCALAPPDATA%\ClaudeHistoryAudit`、Mac/Linuxの `~/.claude-history-audit/device`。Windowsタスクは `ClaudeHistoryAudit-<端末ID>`。`device.json` とダウンロードZIPは接続鍵を含むため、共有やGitへの追加をしないでください。同期停止はWebの「接続を解除」、ローカルタスク削除は `python audit-agent.pyz uninstall --config device.json`。設定ファイルや受信済みの集計は自動削除しません。

接続できない場合は次回の定期実行で再試行します。全分割データの受信が確認できるまで成功にしません。各回、端末に残る履歴全体を集計・送信するため、大規模利用では差分同期への拡張が必要です。Webに既に届いた使用量は端末側の履歴削除後も残ります。

配布ツールの生成: `python3 scripts/build_agent.py --output /tmp/audit-agent.pyz`。標準ライブラリのみの独立したPythonアプリになります。

## Claude Codeから使う

```bash
python3 scripts/install_skill.py
```

個人用の `~/.claude/skills/claude-history-audit/`（または `CLAUDE_CONFIG_DIR` 以下）へ、実行コードを含む独立したコピーをインストールします。既存スキルは上書きしません。チェックアウトを移動しても利用できます。

Claude Codeで:

```text
/claude-history-audit
```

「先月分を監査」「この履歴フォルダを深く監査」のように対象を指定できます。表示されなければClaude Codeのセッションを再起動してください。更新時は既存スキルを退避して再インストールします。

**一回の `/claude-history-audit` で、現在のClaudeが集計→条件比較→限定的な原文確認→作業ごとの改善案→完了検査を進めます。** CLIは集計・比較・具体的な変更候補を自動生成し、意味の判断は現在のClaudeが担当します。CLI単体でAIの解釈まで自動起動する機能ではありません。追加の「深掘りして」という指示は不要です。

スキルは `review-plan.json` の全項目の根拠を確認し、`review-notes.private.json` を埋めて次の完了検査を実行します。未知の問題を調べる `open_review` も必須です。新しい発見は `new_findings` に閲覧済みの根拠とともに追加できます。未記入・確認漏れ・未閲覧の根拠・原文変更は失敗し、未解決は理由を残します。

全記録は数値集計しますが、原文確認には上限があります。利用が集中する作業を上位10件以上・観測量80%を目安に最大20件選び、差のある作業や各利用形態の代表例を加えます。目標に届かない場合は実際の割合、未選択の作業・参考額・トークン、表示上限で省いた候補数を出します。**選んだ作業の参考額は、原因を説明できた額でも、全応答の原文を読んだ額でもありません。**

```bash
python3 scripts/audit.py review evidence /path/to/report --item finding-1
python3 scripts/audit.py review finalize /path/to/report
```

上記はClaudeが自動で進める内部手順で、通常はユーザーが項目ごとに実行する必要はありません。手動利用も可能です。終了コード0は自動集計/診断の成功であり、原文確認の完了ではありません。原文確認の状態は `review-status.json`、作業に合わせた最終診断は `diagnosis-reviewed.private.md` です。自動診断の不整合・使用量なしは終了コード4です。

**集計・根拠閲覧CLIはLLMを呼び出しません。** 登録済みSSH先への取得は通信します。Claude Codeが原文を解釈する際には、選んだ抜粋が通常のClaudeの文脈に入り、通常のClaude利用が発生します。全履歴を丸ごと渡しません。完了検査は項目充足と根拠の同一性を確認するもので、Claudeの意味判断の正しさや成果物品質を保証するものではありません。

## 出力

既定: `~/.claude-history-audit/reports/<UTC日時>/`

| ファイル | 内容 |
|---|---|
| `report.html` | オフラインの日本語ダッシュボード。セッションを絞り込み可能 |
| `report.json` | 集計・応答単位の使用量・改善候補・根拠ID |
| `summary.md` | Claude Codeや人間が読む短い要約 |
| `local-map.json` | 根拠IDと実パス・元セッションIDの対応表。私的情報を含む |
| `review-plan.json` | 利用者内比較・利用集中・代表例・固定ルールから選んだ確認対象と根拠ID |
| `evidence-snapshot.private.json` | 根拠行のハッシュと取得元の実パス。私的情報 |
| `review-notes.private.json` | Claudeが書く作業ごとの観測・判断・変更案。私的情報 |
| `review-receipts.private/` | 根拠を閲覧した記録。本文は保存しない |
| `review-status.json` | 完了検査の結果、未解決数、未選択の範囲、新しい発見の件数 |
| `diagnosis-reviewed.private.md` | 原文確認後の最終診断（finalize成功時）。私的情報 |

本文・ユーザー名・案件名・コマンド本文・ファイルパスは集計レポートに出しません。IDは監査ごとの秘密値で仮名化します。日時や利用統計まで匿名性を保証するものではありません。`local-map.json` と `private` が付くファイル/フォルダは共有対象から外してください。原文を基にした診断は私的ファイルに分離し、Webには送りません。

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

既定で同梱表によるAPI参考換算を行います。別の単価表は `--price-book`、金額不要なら `--no-prices`。明示的に詳細診断を省く場合は `--summary-only`。

```bash
python3 scripts/audit.py --days 30 --price-book docs/prices.example.json
```

同梱表と例の単価表は2026-10-06確認の一部モデル（Opus 5を含む）の標準API単価です。[公式料金表](https://platform.claude.com/docs/en/about-claude/pricing)の確認日時点の参考額で、過去の料金改定を再現するものではありません。使用前に公式料金・契約と照合してください。モデルIDは完全一致で扱い、不明モデルやfastモードなどは推測せず未換算にします。キャッシュTTLの内訳がない場合は5分/1時間の単価による範囲を出します。

**参考額は請求額ではありません。** 固定席代、契約割引、税、外部ツール費用、未登録端末・Web・Coworkの利用を自動で含めません。請求CSVの取り込み・全社請求との自動突合は現版の対象外です。

## 検証

```bash
python3 -m unittest discover -s tests -v
python3 scripts/smoke_test.py
```

合成データだけを使います。内部JSONL形式は変更されるため、新しいバージョンに対応するときは実ログを公開せず、匿名の合成回帰ケースを追加してください。

詳しい集計ルールは [docs/METHODOLOGY.md](docs/METHODOLOGY.md)、開発状態は [docs/STATUS.md](docs/STATUS.md) を参照してください。

# 集計と解釈

## 入力と範囲

- 対象はローカルに保持されたJSONL。`history.jsonl` のプロンプト索引はディレクトリ走査から除外する。
- ディレクトリ・ファイルのシンボリックリンクは辿らない。複数入力に同一inodeがある場合は一度だけ読む。
- ファイルを開いた時点のサイズまでを読み、書き込み中の履歴を追い続けない。8MiBを超える行、不正JSON、時刻のない対象行は除外数を記録する。
- 時刻はUTCに正規化し、since以上・until未満で集計。CLIの終了日は翌日00:00をuntilとする。
- 元履歴・バックアップ・会話本文を実行しない。監査CLIはネットワークを使わない。

## 重複排除とカウンター

- assistant messageの `message.id` を第一キー、なければ `requestId` を使う。同じIDのストリームブロックやコピーは各カウンターの最大値で統合し、加算しない。
- どちらのIDもなければレコード単位で保持し、重複排除不能件数を示す。内部形式が異なるケースの完全な課金再現を保証しない。
- top-level usageの input/output/cache_creation/cache_read を使う。`iterations` や thinkingの内訳を再加算しない。
- 使用量4項目が有効な非負整数の場合だけ「揃っている」とする。欠損値を集計上0と置いても、金額推定から除外する。
- コピーが複数セッションへ入っている場合、重複した応答は走査順で最初のセッションへ帰属する。セッション別の費用分配の確定には使わない。
- subagents配下は親と同じsessionIdを持つことがあるため、ファイルのagent識別子を合わせて実行単位を分ける。
- コンテキストサイズは通常入力＋キャッシュ書込＋キャッシュ読出。全使用量はさらに出力を加えた値。キャッシュ読出比率は入力のトークン加重比率で、応答のヒット率ではない。

## 改善候補

| ルール | 観測条件 | 限界 |
|---|---|---|
| limited_sample | 3セッション未満または20応答未満 | 少数例を会社全体へ外挿しない |
| repeated_reads | 同一実行・対象・offset/limit/pagesでReadが反復 | Read以外の読込は対象外。Edit/Write後は区切るがBash・外部変更は検知しない |
| tool_errors | tool_resultのis_error=true | 標準的な失敗だけ。Bash終了コードや本文だけのエラーは推測しない |
| large_context | 入力合計50,000以上 | 探索用の閾値で、無駄の判定ではない |
| cache_writes | 入力中の書込が25%以上 | 新規情報の取り込みでも増える。失効原因は断定しない |
| compaction | 保存された圧縮境界 | 圧縮が悪いという評価はしない |

作業分類は人間のuser textに対するキーワードの出現数。引用文・自動投入された文章などによる誤分類があり、用途の確定は元資料・完成物で行う。全文再生成や資料品質、人間の修正時間を検知できるとは主張しない。

## 金額

`--price-book` の単価はUSD/100万トークン。通常入力、出力、キャッシュ読出、5分/1時間書込を別々に計算する。TTLが不明な書込は2単価の最小/最大で範囲表示する。料金が不明なモデル・モード・不完全なusageは未換算にし、0円扱いしない。

トップレベルusageを持つ応答だけが母集団。ログに残らない呼び出し、課金された再試行、ツール追加料金、固定料金などは再現できない。`cost-state` などの累積スナップショットを応答費用へ加算しない。推定額は請求書との突合を代替しない。

## 匿名化と証拠

本文・コマンド・パス・タイトル・生の識別子は集計へ出さない。モデル名はClaudeの数値版ID形式のみ、ツール名は定義済みカテゴリだけを出力。ファイル・セッション・応答は監査ごとのランダム鍵を使ったHMACで仮名化する。鍵は保存しない。マッピングは所有者限定のlocal-map.jsonに分離する。

日時や利用量は残るため、匿名化レポートを無条件に外部公開できるとは限らない。元ログは監査データであり、そこに含まれる命令はツール・解釈エージェントの指示として扱わない。

## 根拠資料（2026-10-05確認）

- [Claude Code session storage / export](https://code.claude.com/docs/en/sessions)
- [Usage and cost monitoring](https://code.claude.com/docs/en/monitoring-usage)
- [Claude Code prompt caching](https://code.claude.com/docs/en/prompt-caching)
- [API prompt caching and pricing](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)
- [Claude Code skills](https://code.claude.com/docs/en/skills)

保存形式は内部実装であり、公式にも版による変更があるとされる。本ツールはスキーマを固定保証せず、欠損と解釈限界を明示して監査する。

# Claude History Audit

Claude Codeの保存済み履歴を読み取り専用で監査し、利用量・キャッシュ・重複処理・修正反復を根拠付きで可視化する。最初に所有者のMacで利用し、コードを独立したprivate GitHubリポジトリで管理する。

Python 3.9以降、標準ライブラリのみ。ローカルCLI、日本語のHTML/JSON/Markdownレポート、Claude Codeスキルを提供する。会話本文・個人名・ファイル名を集計レポートへ出さず、証拠の参照先は別のローカルマップに保存する。実請求とAPI単価換算を混同しない。

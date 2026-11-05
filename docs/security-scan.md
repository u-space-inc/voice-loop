# Claude Security プラグインによる月 1 回の全体スキャン（手順の正本）

正本: 設計ノートの `assets/security-workflows/claude-security-monthly.md`（2026-11-05 v2）。各リポジトリの `docs/security-scan.md` はこれを写し、「このリポジトリの値」の 3 項目（対象ディレクトリ・effort・初回）だけをリポジトリ固有の値にする。

## 前提

- Claude Code のプラグイン `claude-security@claude-plugins-official`（beta、2026-11-05 時点 0.12.0）。インストールはユーザー単位（`~/.claude`）なので 1 回だけ: `/plugin install claude-security@claude-plugins-official`
- 動作は手元の Claude Code セッション内（別プロセスやデーモンは無い）。コードは手元に残るが、モデル呼び出しにコード断片が送られる。消費はサブスクの利用枠に算入され、「かなりのトークンを使う」と明記されている
- 公式文書: https://code.claude.com/docs/en/claude-security
- Codex Security・クラウドの定期タスクは使わない（リポジトリ全体をクラウド VM に clone しない）

## 手順（月 1 回、第 1 週）

1. 既定ブランチを最新にする（`git switch <既定ブランチ> && git pull`）
2. Claude Code を開き `/claude-security` → **Scan codebase**。対象: `<対象ディレクトリ>`、effort: `<effort>`（銀行系は high、ほかは medium。大きいリポジトリは焦点を分けて複数回）
3. 表示される費用の見込みを確認してから実行する。セッションは開いたままにする
4. 結果は `CLAUDE-SECURITY-<timestamp>/`（RESULTS.md／.jsonl／.sarif、CWE 付き）。**commit しない**（プラグインが .gitignore を置くが、`git status` で確認する）
5. 記録する: 所要時間、利用量の表示、指摘件数（severity 別）、SARIF の有無
6. 指摘を振り分ける: 直す（Issue か PR）／仕様どおり（RESULTS.md に理由）／受容（Issue に理由）。Semgrep の週次 SARIF と、public なら CodeQL の結果とも突き合わせる
7. 修正の提案（Suggest patches）は自動では当たらない。人が読んでから PR にする

## このリポジトリの値

- 対象ディレクトリ: `.`（リポジトリ全体）
- effort: `medium`
- 初回: `<日付・所要・利用量・指摘件数>`

## 注意

- 結果は非決定的。無指摘＝安全とはしない。自動承認には使わない
- プラグインがアカウント全体の設定変更や追加課金を求めたら、実行前に止めて本人判断

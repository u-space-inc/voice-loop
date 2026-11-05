# Claude Security プラグインによる月 1 回の全体スキャン（手順の正本）

正本: 設計ノートの `assets/security-workflows/claude-security-monthly.md`（2026-11-05 v3）。各リポジトリの `docs/security-scan.md` はこれを写し、「このリポジトリの値」の 3 項目（対象ディレクトリ・effort・初回）だけをリポジトリ固有の値にする。

## 前提

- Claude Code のプラグイン `claude-security@claude-plugins-official`（beta、2026-11-05 時点 0.12.0）。インストールはユーザー単位（`~/.claude`）なので 1 回だけ: `/plugin install claude-security@claude-plugins-official`
- スキャンには Claude Code の Workflow の道具が要る。`/config` に Dynamic workflows の項目があれば有効にして再起動する。項目が無ければ、そのプランでは使えないか、組織のポリシーで無効
- 動作は手元の Claude Code セッション内（別プロセスやデーモンは無い）。コードは手元に残るが、モデル呼び出しにコード断片が送られる。消費はサブスクの利用枠に算入され、「かなりのトークンを使う」と明記されている
- 送ったコードの保持と学習は、アカウントの種類と「モデルの改善への利用（Help improve our AI models）」の設定に従う（個人向けは on で学習に使われ保持 5 年、off で保持 30 日。アカウント全体に効く設定）
- 公式文書: https://code.claude.com/docs/en/claude-security
- Codex Security・クラウドの定期タスクは使わない（リポジトリ全体をクラウド VM に clone しない）

## 手順（月 1 回、第 1 週）

1. **Git 管理下のファイルだけの作業木**を用意する（本体の作業木には Git に入れていない設定値・受領資料・生成物が残っていることがあり、プラグインは作業木を読む）。リポジトリの最上位で:
   - 初回: `git fetch && git worktree add --detach "../$(basename "$PWD")-scan" origin/<既定ブランチ>`
   - 2 回目以降: `git fetch && git -C "../$(basename "$PWD")-scan" checkout --detach origin/<既定ブランチ>`
2. その作業木で Claude Code を開き `/claude-security` → **Scan codebase**。対象: `<対象ディレクトリ>`（これは監査の範囲の指定で、読み取りの制限ではない）、effort: `<effort>`（銀行系は high、ほかは medium。大きいリポジトリは焦点を分けて複数回）
3. 実行前に「時間とトークンをかなり使う」という確認が出る（金額の見積もりは出ない）。60 秒以内に Yes を選ぶ。セッションは開いたままにする
4. 結果は `CLAUDE-SECURITY-<timestamp>/`（RESULTS.md／.jsonl／.sarif、CWE 付き）。**commit しない**。プラグインが .gitignore を置くが、各リポジトリの `.gitignore` にも `CLAUDE-SECURITY-*/` を 1 行足しておく
5. 記録する: 所要時間、利用量の表示（/usage の前後）、指摘件数（severity 別）、SARIF の有無
6. 指摘を振り分ける: 直す（Issue か PR）／仕様どおり（RESULTS.md に理由）／受容（Issue に理由）。Semgrep の週次 SARIF と、public なら CodeQL の結果とも突き合わせる
7. 修正の提案（Suggest patches）は自動では当たらない。人が読んでから PR にする

## 受容する規則（5 本で共通。方針は正本で決める）

- Semgrep `uv-missing-dependency-cooldown`: 扱いは本人判断待ち（2026-11-05。受容して 12 月に uv の設定を検討するか、今から入れるか）

## このリポジトリの値

- 対象ディレクトリ: `.`（リポジトリ全体）
- effort: `medium`
- 初回: `<日付・所要・利用量・指摘件数>`

## 注意

- 結果は非決定的。無指摘＝安全とはしない。自動承認には使わない
- プラグインがアカウント全体の設定変更や追加課金を求めたら、実行前に止めて本人判断
- PR ごとの review 用の secret `CLAUDE_CODE_OAUTH_TOKEN` は、本人が `claude setup-token` で発行して `gh secret set` で登録する。コマンドは値を端末に表示するので、チャット・ログ・記録へ転記しない。有効期間は 1 年なので失効前の予定を入れる

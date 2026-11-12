# Claude Security プラグインによる月 1 回の全体スキャン（手順の正本）

正本: 管理側の `canonical/security-workflows/claude-security-monthly.md`（2026-11-05 v3.2）。各リポジトリの `docs/security-scan.md` はこれを写し、「このリポジトリの値」の 3 項目（対象ディレクトリ・effort・初回）だけをリポジトリ固有の値にする。

## 前提（1 回だけ）

- プラグインを入れる: `/plugin install claude-security@claude-plugins-official`（beta、2026-11-05 時点 0.12.0。ユーザー単位）
- `/config` で Dynamic workflows を有効にする（スキャンは Workflow で動く）
- モデル呼び出しにコード断片が送られ、消費はサブスクの利用枠に入る。保持と学習はアカウントの「Help improve our AI models」の設定に従う（off なら保持 30 日）。公式文書: https://code.claude.com/docs/en/claude-security

## 手順（月 1 回、第 1 週。人がやるのは 1〜3 だけ）

1. リポジトリの最上位で次の 1 行を貼る。Git 管理下のファイルだけの作業木 `../<フォルダ名>-scan` を既定ブランチの最新にして、そこで Claude Code を開く（本体の作業木には Git に入れていない設定値や受領資料が残ることがあり、プラグインは作業木を読むため）
   ```sh
   git fetch -q origin && git remote set-head origin -a >/dev/null && w="../$(basename "$PWD")-scan" && { git -C "$w" checkout -q --detach origin/HEAD 2>/dev/null || git worktree add -q --detach "$w" origin/HEAD; } && cd "$w" && claude
   ```
2. `/claude-security` → **Scan codebase**。対象と effort は下の「このリポジトリの値」。「時間とトークンをかなり使う」の確認に 60 秒以内に Yes。終わるまでセッションを開いたままにする
3. 終わったら管理側のセッションに「スキャンが終わった」と伝える。数字の書き写しは要らない
4. 管理側が集計して記録する
   - 所要・トークン・エージェント数: `~/.claude/projects/<作業木のパス>/<セッション>/workflows/wf_*.json` の `durationMs`・`totalTokens`・`agentCount`
   - 指摘: 作業木の `CLAUDE-SECURITY-<時刻>/`（RESULTS.md・.jsonl・.sarif）の severity 別件数。**commit しない**（`.gitignore` 済み）
5. 指摘を 直す（Issue か PR）／仕様どおり／受容（Issue に理由）に振り分ける。Semgrep の週次と、public なら CodeQL とも突き合わせる。修正の提案（Suggest patches）は自動では当たらないので、人が読んでから PR にする

## 受容する規則（5 本で共通。方針は正本で決める）

- Semgrep `uv-missing-dependency-cooldown`: 受容（2026-11-06）。`uv.lock` があれば版は固定済みで、cooldown が効くのはロックを作り直すときだけ。`pyproject.toml` を読まずに動くスクリプトは、この設定では守れない

## このリポジトリの値

- 対象ディレクトリ: `.`（リポジトリ全体）
- effort: `medium`
- 初回: 2026-11-05（リポジトリ全体・medium）。23.2 分、`totalTokens` 205 万、MEDIUM 2 件

## 注意

- 結果は非決定的。無指摘＝安全とはしない。自動承認には使わない
- プラグインがアカウント全体の設定変更や追加課金を求めたら、実行前に止めて本人判断
- Codex Security・クラウドの定期タスクは使わない（リポジトリ全体をクラウド VM に clone しない）
- PR ごとの review の secret `CLAUDE_CODE_OAUTH_TOKEN` は組織 secret。本人が `claude setup-token` で発行して組織 Settings で登録する（値は転記しない）。有効期間は 1 年なので失効前に更新の予定を入れる

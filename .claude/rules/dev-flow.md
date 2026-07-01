---
name: dev-flow
description: 本リポの開発フロー (4 パターン分岐、 code-review 運用、 Pattern 4 自走手順)。 project rule として auto-load される。
---

# 開発フロー（Git 運用・必須）

2026-06-13 制定・2026-06-25 改訂（GitHub Flow 統一）・2026-07-02 改訂（4 パターン分岐導入）。以降は自走ループ中も含め必ず守る。

## 4 パターンの使い分け

作業・チケットを受け取ったら、 **着手前**に以下のいずれのパターンに該当するか判定する。

| 条件 | Pattern | Issue | PR | code-review (推奨) |
|------|---------|-------|-----|-------------|
| 1-3 行の些細な修正 (typo / 誤字 / 空行 / 純粋な文言整形) | **1** (main 直コミット) | 無 | 無 | 原則無し、 場合により low |
| PR は出したいが Issue 化するほどでない中規模変更 (rule 意味変更 / 依存 minor 更新 / CI 調整 / docs 再編成 / 定型的な reformat) | **2** (Issue 無 + PR 有 + 手動 merge) | 無 | 有 | 原則 medium、 低リスク・狭範囲なら low、 さらに低リスクなら無し |
| 判断介入が必要 (設計選択 / 複雑度 / 新パターン導入 / finding の 1 件ずつトリアージが価値ある) | **3** (Issue + PR + 手動 merge、 default) | 有 | 有 | 原則 medium、 低リスク・狭範囲なら low、 さらに低リスクなら無し |
| 仕様完全 settle 済み + 判断介入不要 + 連続処理可 (パターン化定型実装 / 一括修正 / 命名リネーム / dead code 削除) | **4** (Issue + PR + `/goal` 自走 merge) | 有 | 有 | `medium --fix` 固定 (summary を PR コメント投稿 → 別 issue 起票判断) |

**Pattern 判定に迷ったらユーザーに聞く** — 自己判断で誤ったパターンを選ぶと後の手戻りが大きい。 「たぶん 3 でいいだろう」 で流さず、 明示的にユーザーに確認する。

## code-review level の判定 (推奨提示 + ユーザー判断)

Pattern 2/3 で code-review を通す時 (Pattern 1 も場合により)、 main は規模・リスクを見て**推奨レベル (無し / low / medium) を提示**し、 **最終判断はユーザーに委ねる**。 Pattern 4 は自走のため `medium --fix` 固定 (ユーザー判断は挟まない、 詳細は下述)。

**Pattern 2/3 で code-review を通す時は毎回 `--comment` を付ける** — findings を PR インラインコメントとして残し、 後日振り返り時に「どの行にどの指摘があったか」 を復元できるようにするため。 REFUTED (却下) は自動投稿されないので、 却下は別途手動で PR コメントに記録する。

**Pattern 4 (自走) は `--fix` を base にする** — main が finding を apply/skip 判定 → 明快な指摘を working tree に自動修正。 実行後の summary (「修正した項目 / スキップした項目」) を main が PR コメントとして投稿し、 skip 理由 (a) 意図挙動を変える / (b) 変更範囲超過 / (c) 誤検出 のうち (a) (b) について main が別 issue 起票判断を行う (issue spinoff 運用: milestone 無指定 + needs-triage ラベル付与、 priority phase 後決め)。 `--comment` (inline PR review コメント) は付けない (自走で PR 数が増えるとノイズになるため)。

推奨の目安:

| 変更の性質 | 推奨レベル |
|-----------|----------|
| 純粋な文言整形 / typo / 空行整理 (behavior change 完全ゼロ) | 無し |
| 狭範囲 (1-2 ファイル) + 定型 reformat + 公式仕様準拠 + リスク低 | low |
| 挙動変更を含む / 範囲広め / 新パターン / 規約意味変更 | medium |
| 設計判断混じり / 難易度高 / セキュリティ絡み | medium+ (状況により max/xhigh) |

判定に迷う場合は推奨と根拠を提示し、 ユーザー判断を仰ぐ。 medium と low の境界、 low と無しの境界はどちらもグレーゾーンなので迷いやすい。

## Pattern 1: main 直コミット (微修正専用)

**対象**:

- typo / 誤字 / 空行整理
- コメント文の日本語調整のみ
- 純粋な文言整形 (意味を変えない)

**対象外** (Pattern 2 以上に倒す):

- rule / 規約の意味変更 (文言整形と見紛うが実は意味変わっているもの)
- destructive op を含む変更
- 設定変更 (`.env` / `settings.json` / `package.json` の deps 変更等)
- プロジェクトの監査機構・テスト機構の編集
- 挙動変更を伴う修正 (1 行でも実装のロジック変更は対象外)

**手順**:

1. main 直コミット (Conventional Commits 準拠: `docs:` / `chore:` / `fix:` 等)
2. push

原則 PR / issue / `/code-review` 不要。 ただし 「後で振り返ると意図が読み取りにくい」 「他ファイルに波及影響を確認したい」 等の懸念があれば `/code-review low` をユーザー判断で通してもよい。

## Pattern 2: Issue 無し + PR 有り + 手動 merge

**対象**: 中規模の変更だが Issue 追跡は不要、 レビュー履歴は残したい。

- rule / 規約ドキュメントの意味変更 (文言だけでなく規律の含意が変わる)
- 依存パッケージのマイナー更新 (公式 upstream でも behavior change あり)
- CI / build 設定の細かい調整
- docs 再編成 (複数ファイル書き換え)
- 定型的な reformat (整形ツール適用等、 挙動変わらないが範囲が広い)

**手順**:

1. **ブランチ切り**: `chore/<slug>` or `docs/<slug>` or `refactor/<slug>` (issue 番号を含めない命名)。 **main への直コミット・直 push は禁止**
2. **実装**: main 直 or `code-implement` サブエージェント経由 (判断次第)
3. **PR 作成**: `gh pr create`。 Conventional Commits 準拠のタイトル
4. **code-review level をユーザー判断で決定**: main が推奨提示 → ユーザーが「medium / low / 無し」 を指示
5. **`/code-review <level> --comment` を main 側で独立起動** (「無し」 の場合はスキップ)
6. **findings トリアージ + 修正コミット** (レビュー通した場合)
7. **merge**: `gh pr merge --merge --delete-branch`。 **squash 使わない**

**マージ前提条件**: プロジェクトの test / lint / audit gate green + (レビュー通した場合は) `/code-review` 指摘解消。

## Pattern 3: Issue + PR + 手動 merge (default)

**対象**: 中規模の変更、 判断介入が実装中に発生しうる作業。

- 1 モジュール・1 コンポーネントの実装 or spec 変更
- 設計選択が実装中に揺れる可能性がある
- code-review finding の 1 件ずつトリアージが価値ある (ユーザー裁定が必要)

**手順**:

1. **issue 起票**: GitHub Issue を起票 (機能追加・バグ修正・機構変更すべて)。 Issue 本文に DoD を書く。 既存 issue で対応する場合はそれを選ぶ
2. **ブランチ切り**: `feature/issue-<番号>-<slug>` (例: `feature/issue-12-auth-fix`)。 **main への直コミット・直 push は禁止**
3. **実装**: main 直 or `code-implement` サブエージェント経由 (判断次第)
4. **PR 作成**: `gh pr create`。 Conventional Commits 準拠のタイトル
5. **code-review level をユーザー判断で決定**: main が推奨提示 → ユーザーが「medium / low / 無し」 を指示
6. **`/code-review <level> --comment` を main 側で独立起動** (「無し」 の場合はスキップ)。 fresh perspective 確保、 findings を 1 件ずつユーザー裁定
7. **修正コミット + push**
8. **merge**: `gh pr merge --merge --delete-branch`。 **squash 使わない** (作業コミット単位を main 履歴に残すため)。 `gh pr merge --merge` 自体が `git merge --no-ff` 相当。 **feature ブランチからの直マージ (`git merge --no-ff`) も禁止**
9. **issue close**: DoD 充足を確認して close

**マージ前提条件**: プロジェクトの test / lint / audit gate green + (レビュー通した場合は) `/code-review` 指摘解消。

## Pattern 4: Issue + PR + `/goal` 自走 merge

**対象**: 仕様完全 settle 済み、 判断介入不要、 複数 issue 連続処理。

- パターン化された定型実装 (テンプレート追加、 状態バリアント、 同種の一括修正、 dead code 削除、 命名リネーム等)
- ユーザーは別作業 or 寝てる (無人自走)

**手順**:

1. **issue 起票 (or 既存選択)**: Pattern 3 と同じ。 複数 issue まとめて自走させる場合は全 issue を先に起票しておく
2. **`/goal` プロンプト投入**:
   ```
   /goal <condition>, or stop after N turns
   ```
   構文は `/goal` の公式ドキュメント (<https://code.claude.com/docs/en/goal>) を**逐語厳守**。 `, or stop after N turns` の `or` を省いたり句点・セミコロンで区切ると evaluator が「AND」 と解釈して暴走事故 (実例: 2026-06-25 turn 130+ 暴走)
3. **condition の必須要素**:
   - **`/code-review medium --fix` 通過** (skip して merge されるのを防ぐため必ず condition に含める。 low で足りる作業でも自走中は medium 固定が安全)
   - PR merge
   - issue close

   **例**:
   ```
   /goal Issue #A と #B の PR が /code-review medium --fix 通過後に merge され、両 issue が close された, or stop after 40 turns
   ```
4. **N (turn 上限)**: 作業重量で決定 (`goal-loop.md` 参照)
   - 軽い作業 (issue 1 件、 限定的編集): N=20-30
   - 中量級 (issue 数件、 PR review 込み): N=80-150
   - 重量級 (複数 issue 連続 + PR review fan-out): N=200-230
5. **main の自走**: `code-implement` サブエージェント dispatch → code-implement が実装 + PR 作成 (code-implement 内での `/code-review low` 自律実行は agent 定義書側の規定、 main の手順としては関与しない = 2 段構え運用の下段。 詳細は code-review sub-agent 起動規律 (user rule) 参照)
6. **main が `/code-review medium --fix` 起動** (2 段構え運用の上段):
   - main が findings 抽出 → apply/skip 判定 → 明快な finding を working tree に apply
   - main が最後に「修正した項目 / スキップした項目」 の summary を出力
   - **main が summary を PR コメントとして投稿** (`gh pr comment <PR#> --body "$SUMMARY"` or `gh api repos/<owner>/<repo>/issues/<PR#>/comments -f body="$SUMMARY"`)
7. **skip 記録の後続処理** (別 turn で実行):
   - main が投稿した PR コメントを review:
     - スキップ理由が (a) 意図挙動を変える or (b) 変更範囲超過 → **別 issue 起票** (issue spinoff 運用: milestone 無指定 + needs-triage ラベル)
     - スキップ理由が (c) 誤検出 → 無視
   - 起票結果を PR コメントに追記 (「Issue #N として起票」)
8. **merge → issue close → 次 issue へ**
9. **condition 達成 or turn 上限で stop**

**制約**:
- 起動は `claude --permission-mode bypassPermissions` セッションで行う
- 自作 Stop hook を作らない (`/goal` 自体が Stop hook ラッパーのため衝突)
- evaluator (Haiku) の context 上限に注意 — 大量ファイル touch + メイン直接編集が多い作業は N を抑える、 サブエージェント fan-out 多用パターンは N を伸ばせる (公式仕様: <https://code.claude.com/docs/en/goal>)

## 共通ルール (全 pattern)

### 発見は即 GitHub Issue 化する

セッション中に見つけた未対応の問題・設計負債・改善余地・検証中の派生発見は、 その場で `gh issue create` する。 TaskCreate (タスクリスト) はセッション内の進捗管理用で**セッションをリセットすると揮発する**ため、 跨セッションで残すべき知見の置き場は Issue (or 永続知見なら memory) にする。 「あとで Issue 化しよう」 は揮発の起点なので禁止。

### Conventional Commits

- `feat:` / `fix:` / `chore:` / `docs:` / `refactor:` / `test:` / `style:` 等
- 初回コミットも例外なし

### 参照

- Conventional Commits: <https://www.conventionalcommits.org/>
- Claude Code `/goal` 公式仕様: <https://code.claude.com/docs/en/goal>
- Claude Code hooks 公式仕様: <https://code.claude.com/docs/en/hooks>

Claude Code の user-level rules (main / sub-agent 責務分離、 code-review sub-agent 起動、 issue spinoff、 sub-agent invocation の規律等) は本リポ外で管理されているため、 具体的な運用は各エージェントのセットアップに依存する。 本リポでは本ファイルが Pattern 判定・手順の source of truth。

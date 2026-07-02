# v0.6.1 dogfood test plan

- 主眼: **#273 完成の acceptance criteria 検証** (Catalog / Binder /
  Parser / QueryValidationError 各 reason の wire shape + hint 構造)、
  および `/code-review medium --fix` で apply した 5 finding の
  regression pin。
- v0.6.0 との差分は `run_custom_query` の error envelope のみ。 他
  read tools / import path / async polling 系は無変更なので regression
  section は最小限。
- 過去 dogfood 資産: `v0-6-0-test-plan.md` (v0.6.0 dogfood 計画・
  reason enum / envelope / async polling の観点)、
  `v0-6-0-dogfood-autonomous-results.md` (実測結果、 #273 の起源)。
  ここで挙げた観点で未検証のものは v0.6.0 record を根拠に「差分無し
  = 変わらず PASS」 と扱う。

## 0. セットアップ

前提: `apple-health-mcp-server==0.6.1` を PyPI から取得、 Claude
Desktop から MCP サーバとして起動、 v0.6.0 で import 済みの DB を
再利用 (schema v=6、 records 数百万件)。 fresh-reset は不要 (schema
v=6 のまま)。

環境変数 `APPLE_HEALTH_EXPORT_ZIPS_DIR` は v0.6.0 と同じ設定を継続。

## A. run_custom_query error envelope (メイン)

acceptance criteria: 全 error path で
`{state:"error", reason:<enum>, message, hint?}` (JSON string on wire)
を返し、 `state` を read する前に `rows` を触ってもクラッシュしない
ことを check。 raw `"Error: ..."` prefix が返らないことも同時に確認
(v0.6.0 での misleading CHANGELOG の再発防止)。

### A1. `unknown_table`

- `SELECT * FROM record` (records の typo) → `reason=="unknown_table"`
- `hint.available_tables` に `records` / `workouts` /
  `route_points` / `heart_rate_samples` 含む (main schema の全 table)
- `hint.did_you_mean == "records"` (DuckDB の suggestion parse)

### A2. `unknown_view` (Best effort)

DuckDB は user-defined view 未使用時にこの reason に落ちないので、
`CREATE VIEW` は禁止 (safety.py がブロック) のため、 実機再現は
skip 可。 unit test (`test_translate_catalog_exception_unknown_view`)
で pin されているので wire 経路の pin は不要。

### A3. `missing_column`

- `SELECT hearth_rate FROM records LIMIT 1` →
  `reason=="missing_column"`
- `hint.referenced_column == "hearth_rate"`
- `hint.available_columns.records` に 12 列 (少なくとも
  `record_hash`, `record_type`, `value`, `unit`, `source_name`,
  `device`, `start_date`, `end_date`) 全部含む — DuckDB 自身の
  `Candidate bindings` diagnostic は ~5 個で truncate されるが、
  `information_schema.columns` fallback で完全 list が返ること

### A4. `syntax_error`

- `SELECT * FRM records` (sqlglot が parse エラーで reject) →
  `reason=="syntax_error"`
- `message` に ANSI escape `\x1b[...m` が含まれない (ANSI strip
  済み)

### A5. `empty_query`

- `""` (空 or whitespace only) → `reason=="empty_query"`

### A6. `not_select_or_with`

- `DROP TABLE records` → `reason=="not_select_or_with"`
- 他 DDL / DML (`INSERT INTO ...`, `UPDATE ...`, `CREATE TABLE ...`)
  も同じ reason であること

### A7. `multi_statement`

- `SELECT 1; SELECT 2` → `reason=="multi_statement"`

### A8. `disallowed_function`

- `SELECT * FROM read_csv('/etc/passwd')` →
  `reason=="disallowed_function"`, message に `not allowed` を含む
- `SELECT * FROM 'file:///etc/passwd'` (quoted-path bypass) も同じ
  `disallowed_function` reason に落ちること

## B. `/code-review medium --fix` findings の regression pin

PR #275 で apply した 5 finding を実機で pin する。 全部 unit test
で pin 済みだが、 wire 経路で最終確認。

### B1. A1 fix — did_you_mean が unknown_table/view 以外に混入しない

- `SELECT foo(1)` (unknown scalar function) → `reason` は
  `"execution_error"` に落ちる (`unknown_table` にはならない)
- `hint` が存在するとしても `did_you_mean` キーを含まない (DuckDB が
  emit する "Did you mean 'floor'?" は message にのみ残り、 hint に
  漏れない)

### B2. B1 fix — BinderException 非 `missing_column` variant が
`execution_error` fallback される

- `SELECT record_type FROM records ORDER BY 1, 1, 3` (ORDER term
  out of range) → `reason=="execution_error"` (`missing_column`
  にならない)
- 同じく ambiguous column reference (`SELECT record_hash FROM
  records a JOIN records b USING (record_hash)` の類推) も
  `execution_error` に落ちる

### B3. C2 fix — DESCRIPTION に error envelope shape 記載あり

- Claude Desktop で `run_custom_query` tool description を表示、
  `state`, `reason`, `hint` の 3 語が含まれることを目視確認
- 各 reason enum 値 (`empty_query`, `unknown_table`, etc.) が
  DESCRIPTION に列挙されていることを確認

### B4. ALT1 fix — CHANGELOG.md が factually 正確

- `CHANGELOG.md` の v0.6.0 セクションに「the query path
  (run_custom_query) is NOT translated in this release」 の但し書き
- `[Unreleased]` (v0.6.1 予定) セクションに #273 の記述

### B5. ALT5 fix — BinderException hint が正しい `available_columns` を返す

- A3 と同じ入力で、 hint.available_columns.records の完全性を
  確認 (2 度 parse なくても hint 品質が保たれる = regression 無し)
- self-join (`SELECT a.hearth_rate FROM records a JOIN records b
  ON a.record_hash = b.record_hash`) で `available_columns` に
  `records` のみ (dedup 済み) 出ること

## C. 正常 SQL は壊れない (回帰)

- `SELECT record_type, COUNT(*) FROM records GROUP BY 1 LIMIT 10`
  → 正常な envelope `{rows, row_count, truncated, max_rows,
  user_supplied_limit}` を返す
- `LIMIT 5000` (max_rows 越え) を明示指定 → `truncated:false`,
  `user_supplied_limit:true`
- 巨大 result (`SELECT * FROM records`) → `truncated:true`,
  `row_count == 1000`

## D. Sibling tools は raw string のまま (ALT3 spinoff 前提)

`query_records`, `list_workouts`, `get_correlation_details` 等の
12 tools は v0.6.1 で無変更、 raw `"Error: ..."` prefix を返し続ける
ことを確認。 これは意図的 (#278 で v0.7 に envelope 統一予定)。

- `query_records(record_type="nonexistent")` → `"Error: ..."`
  prefix が返る (envelope shape ではない)。

## X. Adversarial (v0.6.0 の残)

v0.6.0 adversarial (`v0-6-0-adversarial-results.md`) で N1-N8 を
発掘・triage 済。 N1 / N2 / N6 / N8 は v0.6.1 では未対応 (別
milestone)。 v0.6.1 では新規 adversarial は下記のみ実施:

- **AN1**: `SELECT * FROM records WHERE record_hash = (SELECT
  hearth_rate FROM records)` (nested missing_column) →
  hint.available_columns が正しく親クエリの `records` を含む
- **AN2**: `SELECT record_hash FROM records UNION SELECT hearth_rate
  FROM workouts` (UNION の片側で missing_column) →
  hint.available_columns が `records` + `workouts` 両方の全 column
  を含む
- **AN3**: `WITH x AS (SELECT hearth_rate FROM records) SELECT *
  FROM x` (CTE 内で missing_column) → hint.referenced_column が
  正しく `hearth_rate` を parse できる

## 完了判定

- A1 - A8: 全 8 reason が期待通りの wire shape
- B1 - B5: `/code-review medium --fix` の 5 finding が実機で pin
  される (regression 無し)
- C: 正常経路の envelope 無変更
- D: sibling tools が raw string のまま (=意図通り、 #278 で対応)
- X (AN1-AN3): hint 品質が nested / UNION / CTE でも保たれる
- Stop-ship 候補が発生した場合は `internal/dogfood/v0-6-1-dogfood-
  results.md` に記録 → v0.6.2 milestone に spinoff (issue-spinoff
  運用: default milestone 無指定 + needs-triage ラベル)

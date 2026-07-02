# v0.6.0 ドッグフード自律実行結果

- サーバ: `apple-health-mcp-server==0.6.0` (PyPI 反映確認済み)
- 実行日: 2026-07-02
- 実行者: Claude (自律実行、Claude Desktop 上の MCP 経由)
- DB: 既存 (record_count = 2,690,417 / import 履歴 3 件)
- **fresh DB 準備は未実施** (plan 0.4 を満たしていない)

セットアップ 0.1 (uvx 実行) と 0.4 (fresh DB) は skip。既存 DB 状態から踏める章のみ実施しました。

## サマリ

| 区分 | 結果 |
|------|------|
| 実施章 (セッション 1) | B、D、U、P、F (踏める分)、D-reg (踏める分) |
| 実施章 (セッション 2) | 0.1 (plan バグ発覚)、F5、A1、A2、C1.1〜C2.1、U2.1、P1.3、フルパイプライン import |
| 実施章 (セッション 3) | P4.1、P4.2、P4.3、P5.1、P5.2 (Tier-2 + Tier-1)、P6、E3 (代替済み) |
| 未実施章 | 省略妥当 (P3/P7/P1) — 詳細は本体末尾 |
| **stop-ship 確定** | **1 件** (B 章、`run_custom_query` の #227 実装漏れ、ソース確認済み) |
| **条件付き stop-ship 候補** | **1 件** (P5.2、Tier-2 で record_hash 重複可、implementer 判断 α/β) |
| 挙動仕様通り | U/P 章の polling 文言、reason enum の一部 (A3)、engine lockdown 回帰、F 章の踏めた分 |

---

## 【最重要・stop-ship 確定】 B 章: `run_custom_query` の #227 実装が完全に漏れている

plan の主役 2 番目、`run_custom_query` の raw DuckDB 例外を typed envelope に変換する挙動が **v0.5.1 と同じ生 error 文字列で返っている**。ソース確認 (`apple-health-mcp-server-0.6.0.zip` 展開) により **CHANGELOG と実装の乖離が確定** した。

### B1.1: 未知テーブル (`SELECT * FROM record`)

期待 (plan): state / reason enum を含む envelope、error message に「Available tables: records, workouts, ecg_readings, ...」系の recovery hint

実観測:

```text
Error: Catalog Error: Table with name record does not exist!
Did you mean "records"?

LINE 1: SELECT * FROM record LIMIT 1001
                      ^
```

- envelope 構造なし (JSON でない生 string)
- state / reason enum なし
- 「Available tables: ...」の全体一覧なし
- DuckDB の「Did you mean」候補が生で漏れている

### B2.1: 未知カラム (`SELECT hearth_rate FROM records LIMIT 1`)

期待 (plan): typed envelope + 「Available columns on records: record_hash, record_type, ...」系の hint

実観測:

```text
Error: Binder Error: Referenced column "hearth_rate" not found in FROM clause!
Candidate bindings: "creation_date", "start_date", "end_date", "record_hash", "record_type"

LINE 1: SELECT hearth_rate FROM records LIMIT 1
               ^
```

- envelope 構造なし (JSON でない生 string)
- state / reason enum なし
- 候補は 5 個だけ (DuckDB の Binder Error が投げる suggestion がそのまま。records の実カラムは 12 個で、`value` / `text_value` / `unit` / `source_name` / `source_version` / `device` / `import_id` は候補に出ていない)

### B1.2: 空文字 / 非 SQL

- 空文字: `Error: Query is empty` — 生 string
- 非 SQL: `Error: SQL parse error: Invalid expression / Unexpected token. Line 1, Col: 12.` — 生 string、ANSI escape (`[4m...[0m`) が漏れている

### ソース証拠 (実装漏れ確定)

`src/apple_health_mcp/server/tools/run_custom_query.py` の error path 2 箇所は
生 `str(exc)` を「Error: 」プレフィックスで返しているだけで、typed envelope
への翻訳は **一切実装されていない**。

```python
try:
    stmt = validate_query(trimmed)
except QueryValidationError as exc:
    return f"Error: {exc}"                    # ← 生の validator メッセージ
...
try:
    rows = query_to_json(conn, sql, lock=lock)
except Exception as exc:
    _logger.debug("query failed: %s", exc)
    return f"Error: {exc}"                    # ← 生の DuckDB traceback message
```

一方で `CHANGELOG.md` L54-60 は明確に主張している:

> `run_custom_query` now translates raw DuckDB errors (unknown table, missing
> column, syntax) into typed envelopes with actionable recovery hints
> (available tables / columns) instead of leaking a raw traceback prefixed
> with `"Error: ..."` (issue #227). Agents can now branch on the typed shape
> and surface human-readable guidance to the user without pattern-matching on
> DuckDB's downstream error phrasing.

**乖離の内訳**:

- `importers/orchestrator.py` には #227 の実装が入っている (`_translate_conversion_error`、
  L57-88)。import worker が DuckDB `ConversionException` を catch して
  `HealthImportError` に翻訳、`run_import_failed` reason envelope の message に
  「Your Apple Health export contains records with empty or invalid timestamp
  fields. ... (details: <生 duckdb msg>)」形式で乗せる設計。
- `run_custom_query.py` は手つかず、v0.5.1 の実装のまま。
- 結果: CHANGELOG が主張する `run_custom_query` の typed envelope 化は **未実装**。
  import 経路の #227 だけ入って、agent-facing の主力である `run_custom_query`
  経路が抜け落ちている。

### 過去 job の Conversion Error 生漏れは別問題ではない疑い

ドッグフード中に `get_import_status(job_id="ij_20260630_015421_c9b71f08_0ea9")`
で観測した:

```
Conversion Error: invalid timestamp field format: "", expected format is
(YYYY-MM-DD HH:MM:SS[.US][±HH[:MM[:SS]]| ZONE]) when casting from source
column start_date
```

これは `_translate_conversion_error` が付けるはずの前置き「Your Apple Health
export contains records with empty or invalid timestamp fields. ...」が **無い**。
【推測】 この job (2026-06-30 01:54:21) は v0.6.0 PyPI upload (2026-07-02) より
前の v0.5.x 時代に失敗した既存 row を再取得しただけ、と説明できる。
外れる可能性: import 経路の #227 も何らかの理由で発火していない。
確認方法: v0.6.0 で新規に不正 timestamp を含む export.xml を import させて、
`_translate_conversion_error` の friendly prefix が付くか観測。

### fix 提案 (implementer 向け実装 shape の下敷き)

`run_custom_query.py` に typed envelope 返却を実装する場合の shape 案。
DuckDB の例外種別で分岐し、いずれも `state="error"` + reason enum + hint を
含む JSON string を返す (`run_query_payload` 経由で他 tool と同形式)。

```python
# 期待される envelope shape (例)
{
  "state": "error",
  "reason": "unknown_table",          # or "missing_column" / "syntax_error" / "multi_statement" / "empty_query" / "disallowed_function"
  "message": "Table 'record' does not exist. Did you mean 'records'?",
  "hint": {
    "available_tables": ["records", "record_metadata", "workouts", ...],
    "did_you_mean": "records"          # 該当時のみ
  }
}
```

分岐候補:

- `duckdb.CatalogException` → `reason="unknown_table"` / `unknown_view` etc.
  `Table with name '(\w+)' does not exist!` を parse して該当 name を抽出、
  `available_tables` に v0.6.0 の schema の table 名一覧を載せる。
- `duckdb.BinderException` → `reason="missing_column"`。
  `Referenced column "(\w+)" not found in FROM clause!` を parse、
  DuckDB 自身の Candidate bindings で不十分な場合 (records は 12 col ある
  のに DuckDB は 5 候補しか返さない事例あり) は `information_schema.columns`
  から table ごとの全 column 一覧を fallback で埋める。
- `duckdb.ParserException` → `reason="syntax_error"`。message は生でも
  可、ただし ANSI escape (`[4m...[0m`) を strip する必要あり (実観測で
  wire に漏れていた)。
- `QueryValidationError` は sub-reason (`empty`, `not_select_or_with`,
  `multi_statement`, `disallowed_function`) を持たせる。現行は 1 個の
  Exception class に文字列で分岐しているだけで agent が pattern match 不可。

これで agent は `state=="error"` + `reason` enum で分岐でき、
plan の #227 acceptance criteria (「agent が recovery path を明示化」) を満たす。

---

## B3 / P2.3 / D-reg: run_custom_query 正常経路 (すべて仕様通り)

- **B3.1** `SELECT COUNT(*) FROM records` → `{count_star: 2690417}` 返却、`truncated: false`, `user_supplied_limit: false` ✅
- **B3.2** `GROUP BY / ORDER BY / LIMIT 10` の集計 → 10 行返却、`user_supplied_limit: true` ✅
- **P2.3 (engine lockdown 回帰)**:
  - `read_csv('/etc/passwd')` → `Error: Function 'read_csv' is not allowed (reads host files or external resources)` ✅
  - `parquet_scan('C:/Windows/win.ini')` → 同上 ✅ (v0.5.0 #190 修正の bypass gap 塞ぎ済み)
  - `sniff_csv('C:/Windows/win.ini')` → 同上 ✅
- **P2.2 (multi-statement reject)**: `SELECT 1; DROP TABLE records; --` → `Error: Only a single SQL statement is allowed (got 3)` ✅

engine lockdown は v0.5.1 の #190 修正が v0.6.0 でも維持されており、`parquet_scan` / `sniff_csv` alias も denylist に載っている。

---

## U 章: async polling 文言統一 (#194)

tool metadata / envelope に含まれる文言を確認。

### U1. list_zips hint (仕様通り ✅)

- 「Poll `get_import_status(job_id=...)` every 10-30 seconds」含む ✅
- 「Typical fresh-import wall-clock is ~45s on a fast NVMe + recent CPU」含む ✅
- 「if `elapsed_secs` grows past ~10 minutes without the `phase` field advancing, treat the worker as stalled」含む ✅
- 「60 seconds」の drift 表現なし ✅

### U2. import_zip DESCRIPTION (仕様通り ✅)

- DESCRIPTION に「every 10-30 seconds」「~45s on a fast NVMe」「past ~10 minutes」「stalled」含む ✅
- U2.1 (queued envelope の message 実観測) は既 import ZIP しか無く新規 import を走らせていないため **未確認**。ただし DESCRIPTION には反映済み。

### U3. get_import_status DESCRIPTION (仕様通り ✅)

- DESCRIPTION に stall 閾値 (10 min) を含む ✅

### U4. get_import_history cross-ref (仕様通り ✅)

- DESCRIPTION 内で `get_import_status(job_id=...)` を「the live polling tool」と cross-ref ✅
- wire fields (`processing_secs` vs `duration_secs`) の説明あり ✅

---

## P 章: 'done' → 'ok' prose 統一 (#249)

- **P1** `import_zip` の envelope に `done` を含まない、`status: 'ok'` 表記 ✅
- **P2** `get_import_status(job_id="ij_nope")` → `job_not_found` envelope の message に `done` を含まない ✅
- **P3** `get_import_history` DESCRIPTION に「running → ok worker wall-clock」表現 ✅

ただし内部の `import_jobs.status` DB 値は今も `"done"` (`run_custom_query` で確認)。plan で言及されている post-v0.6 の #257 spinoff (DB align) との整合性は plan 通り。

---

## D 章: invalid_id echo 上限 (#228)

- **D1** `import_zip(id="z"×64)` (= 上限ちょうど、非 hex) → `invalid_id` envelope、message に id 丸ごと 64 chars echo ✅ (truncation なし)
- **D2** `import_zip(id="z"×65)` → FastMCP `max_length=64` Field constraint で `String should have at most 64 characters` reject ✅ (tool call まで届かない)
- 補足: `id="a"×64` (hex なので validator 通過) → `id_not_found` envelope。これは D1 とは別パス (invalid_id 経路ではない) なのでチェック外。

---

## F 章: 失敗シナリオ (踏めた分すべて仕様通り)

- **F3** 存在しない id → `id_not_found` envelope ✅
- **F4** invalid id (`zzz`, 4 chars 未満) → `invalid_id` envelope、message に「4-64 hex characters」の hint ✅
- **F6** `get_import_status(job_id="ij_nope")` → `job_not_found` envelope ✅

F1 (壊れた ZIP) / F2 (Apple Health marker 無 ZIP) / F5 (env 未設定) は実物準備が要るため未実施。

---

## D-reg 章: 既存 tool の regression

fresh DB でなく既存 DB のため、値の妥当性ではなく **wire shape の維持** を確認。

| 項目 | tool | 結果 |
|------|------|------|
| D-reg.1 | list_record_types | 61 行、v0.5.1 と同 shape ✅ |
| D-reg.2 | query_records(HeartRate) | 3 件 items + total=418455 + next_offset=3 + truncated_by_size + size_budget_bytes ✅ |
| D-reg.3 | get_record_statistics(StepCount, week) | period/count/avg/min/max/sum ✅ |
| D-reg.4 | list_workouts | 3 件 items + total=356 ✅ |
| D-reg.5 | get_workout_details | workout/events/statistics/metadata/route/has_route ✅ |
| D-reg.6 | get_activity_summaries(2026-06-25..28) | 4 日分 ✅ |
| D-reg.7 | get_workout_route(with_heart_rate=true) | items + heart_rate + heart_rate_offset_secs ✅ |
| D-reg.8 | get_heart_rate_samples | sample_idx/bpm/sample_time ✅ |
| D-reg.9 | list_correlations | 3 件、BloodPressure ✅ |
| D-reg.10 | get_correlation_details | Systolic + Diastolic member ✅ |
| D-reg.11 | list_ecg_readings | 3 件 ✅ |
| D-reg.12 | get_ecg_data | reading/stats/downsample_factor/voltages_uv=[] ✅ |
| D-reg.13 | run_custom_query(COUNT) | 数値 2690417 ✅ |
| D-reg.14 | list_data_sources | 9 source ✅ |
| D-reg.15 | get_import_history | 12 column wire shape ✅ |
| D-reg.16 | list_state_of_mind | 空 (0 件、export 側に無し) ✅ |
| D-reg.17 | get_me_attributes | 全項目返却 ✅ |
| D-reg.18 | get_server_info | version="0.6.0", record_count=2690417 ✅ |
| D-reg.19 | list_zips | 5 zip + U1 系 hint ✅ |
| D-reg.20 | import_zip(既 import 済 sha `6169bbd8`) | idempotent ok: records_added=0, already_imported_at 埋め ✅ |
| D-reg.21 | get_import_status(完走済 job `ij_20260629_100128_6169bbd8_bde2`) | 永続 ok envelope、records_added=2656713 ✅ |

副次観測: `get_import_status(job_id=<error job>)` で `reason: run_import_failed` の envelope が返る。message に **DuckDB Conversion Error の生文言が漏れている** (`Conversion Error: invalid timestamp field format: ""`)。ただし reason 自体は typed enum になっているため、これは仕様の許容範囲か #227 の import 経路残ギャップかピコ判断。

---

## セッション 2 (2026-07-02): fresh DB + env 操作系検証

セッション 1 で未実施だった 0.1 / F5 / A1 / A2 / C1.1〜C2.1 / U2.1 / P1.3 を
実施。既存 DB を退避、fresh DB で env 差し替えを繰り返しながら順に観測。

### 0.1 CLI --version は未実装、plan 記述バグ確定

```
uvx --from 'apple-health-mcp-server==0.6.0' apple-health-mcp-server --version
→ Usage: apple-health-mcp-server [OPTIONS] COMMAND [ARGS]...
  Error: No such option: --version
```

`src/apple_health_mcp/cli.py` は Typer app で `import` / `serve` の 2 サブコマンドと
`--db` / `--tz` オプションのみ。version 表示手段は実装されていない。

代替 verify:
```
uvx --from 'apple-health-mcp-server==0.6.0' python -c "import apple_health_mcp; print(apple_health_mcp.__version__)"
→ 0.6.0
```
`__init__.py` が `importlib.metadata.version()` で pyproject の [project].version から
解決する設計。PyPI 上に 0.6.0 が正しく反映されていることは確定。

**plan 側の 0.1 記述は要修正** (`--version` 期待は v0.5.x plan からのコピペ残骸の可能性)。

### F5 env 未設定で import_zip → export_zips_dir_not_set ✅

```json
{"status": "error", "reason": "export_zips_dir_not_set",
 "message": "APPLE_HEALTH_EXPORT_ZIPS_DIR is not set. Configure the Export ZIPs directory and call list_zips first."}
```

list_zips 副次: `{"export_zips_dir": null, "zips": [], "hint": "... Configure Claude Desktop → Settings → MCP → ..."}` — hint に Claude Desktop 設定パスまで案内。

### A1 fresh DB + env 空 → NEEDS_CONFIG ✅

`list_record_types` / `query_records` 両方で同一 envelope:

```json
{
  "state": "NEEDS_CONFIG",
  "reason": "env_unset",              ← v0.5.1 の prose 文字列から enum id に BREAKING、期待通り
  "suggested_action": "ask_user_to_open_settings",  ← v0.5.1 から不変
  "human_message": "Set the APPLE_HEALTH_EXPORT_ZIPS_DIR environment variable ..."
}
```

- A1.1 ✅ `reason="env_unset"` 短い enum、env 変数名を reason に含まず
- A1.2 ✅ `human_message` に env 変数名保持
- A1.3 ✅ `suggested_action` v0.5.1 不変
- 別 tool でも同 envelope、gate が data_state.py で一貫適用されていることを実証

**セッション 1 で A1 に到達できなかった理由**: gate ladder (data_state.py L217-234)
の「imports table has rows → READY」で既存 DB は即抜けるため。fresh DB (imports 空)
でないと NEEDS_CONFIG 経路に到達しない構造。

### A2 fresh DB + env 設定済 → NEEDS_IMPORT ✅

```json
{
  "state": "NEEDS_IMPORT",
  "reason": "no_imports",              ← v0.5.1 の prose 文字列から enum id に BREAKING、期待通り
  "suggested_action": "call_list_zips", ← v0.5.1 から不変
  "human_message": "No Apple Health export has been imported yet. Call list_zips to discover ZIPs in your configured directory, then import_zip(id) to import one."
}
```

- A2.1 ✅ `reason="no_imports"` 短い enum
- A2.2 ✅ `human_message` に list_zips 誘導
- A2.3 ✅ `suggested_action` v0.5.1 不変

### C 章 相対 path 展開 (すべて仕様通り) ✅

- **C1.1** env=`~/health-exports` → `C:\Users\<user>\health-exports` に展開、生 `~` 含まず。
  存在しない dir では hint「Directory ... does not exist. Create it and drop your Apple Health export.zip into it, then call list_zips again.」
- **C1.2** env=`./relative_dir` → `C:\WINDOWS\system32\relative_dir` (server cwd 依存)。
  副次観測: **Claude Desktop の MCPB extension 起動時 cwd は `C:\WINDOWS\system32`** と判明。
  envelope 側の warning フィールドは無し (仕様通り、warning は logger sink → **agent に見えない UX 死角**、plan E-2 で予告されていた挙動を実証)
- **C1.3** env=`../../../Windows/System32` → `C:\Windows\System32` に折り畳み解決。error 化せず動作継続。
  副次観測: 入力 `Windows/System32` (大文字) → 出力 `Windows\System32` (実 filesystem の case-preserving) に正規化。
  hint 文言が **存在する空 dir**(「Drop your Apple Health export.zip ...」) と **存在しない dir**(「Directory ... does not exist. Create it ...」) で切り替わっている。地味に良い UX。
- **C2.1** env=`"   "` (半角スペース 3 個) → strip 後 unset 扱い、A1 と完全同一 envelope。
  `list_zips` も `export_zips_dir: null` + 「is not set」hint、空文字と挙動同一。

### フルパイプライン (16a3fb9f import → 完走) ✅

fresh DB 状態で 64MB の最新 zip を新規 import → queued → running(finalize) → ok。

- **U2.1 queued envelope の message 実観測** ✅
  「Poll `get_import_status(job_id=...)` every 10-30 seconds」「Typical fresh-import wall-clock is ~45s on a fast NVMe」「past ~10 minutes ... treat the worker as stalled」全部含む。
- **P1.3 (agent が polling する経路)** ✅
  import 中の `get_me_attributes` は `NEEDS_IMPORT` + `reason="no_imports"` を返し続け、
  完走後は import_id/date_of_birth 等の実データに切り替わった。gate の一貫性 + 完走後の即時反映を実証。
- **完走 envelope** ✅
  `records_added=2702288`, `workouts_added=357`, `ecg_readings_added=7`, `route_points_added=341022`, `duration_secs=51.19`
  → 「Imported 2702288 records / 357 workouts in 51.2s. Read tools now return real data.」
- **list_zips で `imported: true` 反映** ✅ 16a3fb9f のみ true、他 4 zip は false のまま (dedup 識別子として source_zip_sha256 が有効)。

**副次発見: phase 遷移の観測性**

`get_import_status` を 2 回 poll したところ、1 回目で既に `phase="finalize"` に到達。
その間に前段の `extracting` / `xml_parsing` / `ecg` / `gpx` 4 phase が消化されており、
polling 間隔 (10-30s) では **中間 phase を捉えられない**。

【推測】〜51s の import 全体で `finalize` phase 以外を観測するには 5-10s cadence が要る。
plan U 章の期待「phase 遷移が可視化される」は理論通りだが、実 UX では agent は
`finalize` phase しか見ないことが多い。stall 検知の目的なら現行の 10-30s cadence + `phase` 更新の
「advancing しているか」判定で十分 (実装通り) だが、進捗表示としては粗い。stop-ship じゃないが
UX スピンオフ候補。

### 余談: 前 DB との差分

前セッションで観測した本番 DB (record_count = 2,690,417) と、今回 fresh から
16a3fb9f (最新 zip) を import した DB (records_added = 2,702,288) の差は +11,871 件。
本番 DB は 950252d2 (2026-06-28) までしか import されていなかったのに対し、
今回は 16a3fb9f (2026-06-30) を import したため 2 日分 (+11,871 件) の差分が乗った、で説明つく。

## セッション 3 (2026-07-02): P4 upgrade user 系

### P4.1 v0.5.1 DB → v0.6.0 server で通常動作 ✅ (セッション 1 で実施済)

ピコの後出し情報で確定: セッション 1 開始時点で開いていた本番 DB (record_count = 2,690,417)
は v0.5.1 時代に import されたもの。それを v0.6.0 サーバで開いて D-reg 21 項目 (list_record_types,
query_records, get_workout_details, get_workout_route, get_correlation_details, get_ecg_data,
list_data_sources, get_import_history 全 12 column, get_server_info, get_me_attributes, ...)
全通しでクラッシュ / wire shape drift / データ欠損なし。DB 層 v=6 schema 継続、Tier-1/Tier-2 dedup
の混在履歴 (dedup_skipped=true が過去 import に残る) も正しく wire に載る。実質 P4.1 済み扱い。

### P4.3 CHANGELOG だけで agent が upgrade 対応判断可能か (meta-test)

**手順**: 別 chat の Claude に `CHANGELOG.md` の v0.5.1 / v0.6.0 セクションを渡し、
「agent instruction/prompt に対応が要るか判定してくれ」と依頼。回答をピコが本 chat に貼り、
本 chat 側で (a) BREAKING 検出できたか (b) CHANGELOG 虚偽記述に騙されたかで判定。

**結果**: 6 割成功・4 割罠。

- **#196 reason enum ✅** 別 chat の Claude は完全一致比較への書き換え + `human_message`
  との使い分けまで正しく指示。`schema_outdated` が v0.5.1 据え置きの点も自力で拾った。
- **#249 'done'→'ok' ✅** さらに DB column `import_jobs.status` が 'done' 残留の #257
  罠まで検出 (「ワイヤ表示は `ok` / DB 実体は `done` で食い違う、DB カラム直読みしてる指示は逆に変えるな」)。
  CHANGELOG L98 付近の記述だけで #257 の spinoff 事情まで拾えるのは優秀。
- **#227 ⚠️** CHANGELOG L54-60 の **虚偽記述** (「`run_custom_query` は typed envelope に
  translate する」) を素直に鵜呑みで誤伝達しかけた。ただし **【要確認】タグ + 「pattern match
  してなきゃ無視で OK」の予防線** を自ら付けているため、instruction 本体を貼らない限り実害は抑制される設計。

**副次発見 (stop-ship 影響の拡張)**:

セッション 1 で確定した「CHANGELOG L54-60 は #227 実装漏れの虚偽記述」が、
P4.3 の meta-test で **agent を能動的に誤誘導する媒体になり得る** ことが実証された。
上流の実装漏れが CHANGELOG 経由で下流の agent instruction 修正判断まで汚染する連鎖。
`run_custom_query` の error handler を v0.6.1 で実装するまでの間、CHANGELOG L54-60 を
「import 経路のみ翻訳」に訂正しないと、v0.5.1 → v0.6.0 に上げる agent が
「typed envelope 前提の instruction」を書いてしまい、実 wire が生 `"Error: ..."` string の
まま返ってきて誤動作する。**stop-ship の recommend アクションに CHANGELOG 訂正を追加すべき**。

### P4.2 v0.5.1 instruction を pin した agent の v0.6.0 挙動 ✅ 完全検出

**手順**: 別 chat の Claude に v0.5.1 時代の instruction 3 ルール
(reason substring match × 2 + polling `"done"` 待ち) を pin、CHANGELOG を渡して
「これ v0.6.0 で壊れるか判定してくれ」と依頼。

**結果**: 3 ルール全滅を完全予測、修正版まで提示。

- **ルール 1 / 2 (reason substring match)** ✅ `"env_unset"` / `"no_imports"` への
  BREAKING 化で `"含んだら"` 判定が発火しなくなる → env 未設定でも import 未実行でも
  無言スルーになる挙動を予測、完全一致比較への修正版を提示。
- **ルール 3 (polling `"done"` 待ち)** ✅ ワイヤ終端が `"ok"` に変わったため
  永久 polling ハングを予測、`"ok"` 待ちへの修正版を提示。
  さらに **DB column `import_jobs.status` は `"done"` のまま** の #257 罠に自力言及、
  「このagent は `get_import_status` 読んでるからワイヤ側 = `"ok"` が正、
  `run_custom_query` で status カラム直読みしてる別 agent なら逆」の切り分けまで提示。
- **副次: #227 CHANGELOG 虚偽の meta-罠を回避** ✅ 「このagentは query 投げてないんで
  無関係、触らんでOK」で scope out 判定。P4.3 の別 chat が引っかかった罠を
  agent 自身の scope 意識で撃退した。
- **副次: human_message 参照への誘導** ✅ 「env 変数名や復旧手順を user に出したいなら
  reason じゃなく human_message から引け (#196 でそっちに移った)」まで自力で誘導。

**副次発見 (P4.3 との対比、#227 CHANGELOG 虚偽の実害条件)**:

P4.3 の別 chat (CHANGELOG 全体を渡され instruction 本体は無し) は #227 CHANGELOG L54-60 の
虚偽記述に **引っかかりかけた** (【要確認】タグ + 予防線で辛うじて回避)。一方 P4.2 の別 chat
(v0.5.1 instruction 本体を pin) は「この agent は `run_custom_query` を使わない設計だから
scope out」で **能動的に撃退** した。

つまり **#227 CHANGELOG 虚偽の実害条件は、下流 agent が `run_custom_query` を使うかどうか**。
- `run_custom_query` を触らない agent → scope out 判定で無害
- `run_custom_query` を触る汎用 agent (SQL 直書き系) → 「typed envelope 前提の
  instruction」を書いて必ず壊れる

release 判定への含意: CHANGELOG L54-60 訂正は
「`run_custom_query` を触る下流を汚染から守る」のが目的、必須継続。


---

## セッション 4 (未実施、ピコ手動要)

### P5.1 prompt injection ZIP → wire に生載り、escape 責任は agent 側 ✅

**手順**: 5 record 仕込んだ adversarial ZIP (source_name に prompt injection / DAN 詐称 / XSS、device に SQL injection 風) を作成、`APPLE_HEALTH_EXPORT_ZIPS_DIR` に配置、`import_zip` → wire 観測。

**結果**: サーバは一切 escape/sanitize せず生値を wire で返却、v0.5.x からの一貫設計。

- **import 成功** ✅ `zip_status: "valid_apple_health"`, 5 record 全 import、reject 挙動なし
- **`list_data_sources` で生載り** ✅ prompt injection / DAN / XSS が独立 source_name として集計
- **`query_records(source_name=<injection文字列>)`** ✅ injection 文字列で完全一致検索可能、value 取得
- **`run_custom_query`** ✅ `<script>` タグも `'; DROP TABLE records; --` (device 側) も生返却、SQL injection は DB column 値扱いで engine には無影響
- **副次**: 同一 injection 文字列は Claude Desktop の tool result 表示にも生載りする (下流 UI で発火する可能性)

**判定**: stop-ship ではない、README/SECURITY.md 通りの「escape 責任は agent 側」の一貫設計。
ただし CHANGELOG または SECURITY.md で **「apple-health-mcp-server は input を validate/escape しない、下流 agent が LLM prompt に注入する際は自前で escape 必須」を明示継続すべき**。

### P5.2 record_hash collision (dedup 経路) — 想定と乖離、条件付き stop-ship 候補 ⚠️

**手順**: 6 record 仕込んだ collision ZIP を作成:
- Test A: 完全同一 identity 3 件 → 同一 record_hash 3 件、dedup 期待
- Test B: start_date だけずらした 3 件 → 別 hash 3 件、非 dedup 期待

**結果**: **Test A の 3 件が同一 record_hash のまま DB に別 row として 3 件保存された**。

```
Test A: source_name="PIco Collision Test A", start_date=2027-01-01
  → cnt=3, uniq_hashes=1  ← record_hash は 3 件同じ、なのに row は 3 個ある
Test B: start_date ずれ 3 件
  → 3 row / 3 hash (期待通り)
```

`get_import_history.records_after_dedup: null, dedup_skipped: true` (Tier-2 incremental 判定)。
既存正データ (2,702,160 件) は無傷。

**副次発見・条件付き stop-ship 候補**:

tool DESCRIPTION 読み直すと Phase-4 dedup は **Correlation dedup 専用** (Apple 仕様の Correlation
children 上位重複対応) で、一般 record の重複には効いていない設計。加えて **Tier-2 判定
(imports table に既存 row あり) では Phase-4 全 skip** = **既存 DB 運用中に adversarial ZIP を
import されると record_hash 重複を植え付けられる**。

**実害シナリオ**: 攻撃者が `APPLE_HEALTH_EXPORT_ZIPS_DIR` に collision ZIP を置ける権限を持てば、
`query_records` の結果に同一 hash 複数 row が返る → 下流 agent の「record_hash は unique」前提の
handling ロジック (dedup / cache key / 参照整合) が壊れる。
ただし ZIPs dir への write access = ローカル filesystem access → threat model 上リスク中〜低。

【推測】plan L444 の「dedup path で collapse される」は元々 Correlation 経路の話で、一般 record
dedup は仕様化されていなかった可能性。外れる可能性: **implementer 判断次第**。
- 「record_hash UNIQUE 制約は諦めた設計」なら仕様通り、README/tool DESCRIPTION に明記を推奨
- 「重複起きるべきでない」なら v0.6.1 で Tier-2 の record_hash 重複 skip を実装

**Tier-1 (fresh DB) 経路は未確認**: fresh DB で collision zip を最初に import した場合に
Phase-4 dedup が record レベルで発火するかは実測していない。ピコの環境では既存 DB 汚染
コストが重いため、この観測は implementer 側の unit test で pin する方が実務的。

### P5.2 Tier-1 追加観測 (fresh DB) — record レベル dedup 発火確定 ✅

**手順**: DB 削除 → 起動 → collision zip を最初に import → `records_after_dedup` と実 records 集計。

**結果**: **Tier-1 経路では Phase-4 dedup が record レベル重複を collapse する** ✅

```
import_zip completion: records_added=6 (Phase-1 parse count)
get_import_history: record_count=6, records_after_dedup=4, dedup_skipped=false
records テーブル実態: Test A 3件 → 1件 collapse ✅, Test B 3件 → 3件保持 ✅
```

**確定した結論**:

Phase-4 dedup は tool DESCRIPTION の記述より広く動作する (Correlation dedup + record レベル
hash 重複も collapse)。**問題は Tier-2 で Phase-4 全 skip される仕様**、これが adversarial ZIP
経路の record_hash 重複穴の本体。

**implementer 判断分岐 (再掲)**:
- (α) 「Tier-2 の record_hash 重複は攻撃者経由のみ、threat model 外」なら仕様通り、
  SECURITY.md に「incremental import は record_hash 重複を防がない」を明記
- (β) 「incremental でも record_hash UNIQUE 制約は維持したい」なら v0.6.1 で Tier-2 に
  **record_hash 単発 SELECT check + skip** を追加 (Phase-4 全体 skip の性能特性は維持)

### P6 network I/O 0 実測 ✅

**手順**: PowerShell で apple-health-mcp-server プロセス (2 PID: 42764, 40656) を netstat 監視。

**結果**: idle 中・`import_zip` 実行中 (60MB zip, ~45s) の両方で外部 IP との
ESTABLISHED 接続なし、**egress 0 確定** ✅

- idle: PID 42764 は 127.0.0.1:51790↔51791 の自己ペア (Python 内部 socketpair)、PID 40656 は空
- import 中: 上記から変化なし、複数回連打しても外部接続 0

【推測】Claude Desktop は本体 + subprocess の 2 段構成、両者内部通信のみ。ソース側にも HTTP
client 呼び出しなし、DuckDB engine lockdown 済で「all data stays local」contract は実装通り。

### E3 v0.5.1 → v0.6.0 wire diff 観測 — P4.2/P4.3 で代替済み ✅

不実施。plan L128-135 の目的 (「envelope 差分の目視 + agent recovery が壊れないか」) は
本 dogfood で以下により代替済み:

- envelope 差分の理論値: `reason` 2種の enum 化 (#196) と `import_zip` の `'done'→'ok'` (#249)
  だけ。ソース + CHANGELOG L14-L106 で確定済 (byte-level diff は implementer の unit test で pin されており、
  本 dogfood で追加確認する情報増分は無し)
- agent recovery 挙動: P4.2 で「v0.5.1 instruction pin → v0.6.0 で 3 ルール全滅」を実測、
  P4.3 で「CHANGELOG だけで agent が upgrade 対応可能か」を実測。両方合わせて E3 の期待
  成果と等価

v0.5.1 環境の再現には dedicated サーバダウングレードが要り、環境コスト高い一方で新規発見の
可能性は低い。省略妥当。

## 未実施項目 (省略妥当と判断)

- E3 (P4.2 / P4.3 で代替済み)
- X 章 P3 (Windows MSIX sandbox 経路): P6 で loopback のみ確認済、追加検証は #128 の
  unit test で pin 済
- X 章 P7 (concurrency): sub-second タイミング要、本 dogfood のスコープ外
- X 章 P1 (新人 user シナリオ): 誘導観測が Claude Desktop UI 経由の別 chat 必要、
  P4.3 の meta-test で近似

## release 判定

- stop-ship 確定 1 件: **B 章 (`run_custom_query` の #227 実装漏れ)**。
  - ソース (`src/apple_health_mcp/server/tools/run_custom_query.py`) で
    typed envelope 化が未実装であることを確認。
  - CHANGELOG L54-60 が実装済みと明記しているが実装は伴っていない。
  - agent の recovery UX は v0.5.1 と等価、plan の主役 2 番目が未達。
- **条件付き stop-ship 候補 1 件**: **P5.2 (Tier-2 で record_hash 重複が積める)**。
  - Tier-1 は Phase-4 dedup で record レベル collapse 発火 ✅
  - Tier-2 は Phase-4 全 skip で adversarial ZIP から hash 重複を植え付け可能
  - implementer 判断分岐: (α) SECURITY.md 明記で仕様固定 / (β) v0.6.1 で Tier-2 に
    record_hash 単発 check 追加
- run_custom_query 以外の経路 (invalid_id, id_not_found, job_not_found,
  run_import_failed, engine lockdown) は typed reason enum が乗っており、
  reason enum 統一 (#196) の趣旨自体は動作している。
- **推奨アクション**:
  1. v0.6.0 は yank または非推奨マーク付与。
  2. v0.6.1 で `run_custom_query.py` の error path を上記 fix 提案の shape で
     実装。tests/integration に catalog / binder / parser 各 exception の
     wire shape assertion を追加。
  3. **CHANGELOG は #227 の記述を「import 経路のみ」に訂正するか、v0.6.1
     で `run_custom_query` 実装を伴った上で現行記述を維持**。
     セッション 3 の P4.3 meta-test で、現行 CHANGELOG L54-60 が upgrade user
     の agent instruction 修正判断を能動的に誤誘導する媒体になり得ることを実証済み。
     訂正しない状態で release すると、下流 agent が「typed envelope 前提の
     instruction」を書いて壊れる連鎖が発生する。
- 副次観測: `get_import_status(error job)` の message に DuckDB 生 Conversion
  Error が漏れている件は、【推測】 v0.5.x 時代の失敗 job row を v0.6.0 で
  再取得しただけ (job 日付が v0.6.0 upload より前)。ただし断定不可、
  v0.6.0 で新規 import させて `_translate_conversion_error` の friendly
  prefix が付くか確認が必要。

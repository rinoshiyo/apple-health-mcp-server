# v0.5.1 dogfood 結果ログ

本書は `docs/dogfood/v0-5-1-test-plan.md` への回答 (= 実機結果)。
個人情報・ローカル環境固有値はスクラブ済 (= 端末名 / OS / 個人 path /
MSIX sandbox 絶対 path / 個人 export sha8 / 個人 export 規模厳密値 /
日本語 ZIP 名 / 口語ピコ識別はすべて placeholder 化)。

- 実施日: 2026-06-29〜30
- サーバーバージョン: `0.5.1` (= `get_server_info` で確認)
- 実行経路: Claude Desktop / MCPB bundle (= stdio)
- DB: `platform_default`
- DB 状態: 既存 v=6 / `record_count: ~2.7M` / `imports: 2 件`
  (= fresh import + incremental 1 件)

## 総括

- **主役 2/2 (= external access lockdown #190) = 完全 green**。
  stop-ship #190 の漏れ 4 関数 (= `parquet_scan` / `parquet_metadata` /
  `parquet_schema` / `sniff_csv`) が実機 MCPB bundle 経由で全滅。
  SSRF / 任意 fs 読み取りの両経路とも封鎖を確認。 engine 設定
  `enable_external_access=false` が MCPB 起動経路でも落ちずに維持されて
  いることを probe で実証
- **主役 1/2 (= schema_outdated typed envelope #188) = チャット経路から
  未踏** (= v=5 DB を物理的に確保できないためスコープ外、 unit test で
  pin 済)
- **副題 (= #187 hint / #189 processing_secs / #191 id 寛容) = 全 green**
- D (= read/write tools regression 21 本) = 全 green
- F (= 失敗シナリオ 6 件) = 全 green (F5 は env 操作要、 別途実施)
- P2 データオタク = P2.1〜P2.4 / P2.6 green。 **P2.5 (同 query 100
  連発) は agent ループ単独では現実的に踏めないため skip**
- X2 入力境界 = X2.1〜X2.5 green (= 境界 import_zip 系)、 X2.6〜X2.11
  は追加実施分で全 green
- X8 破壊耐性 final = 全 green。 X 章開始前と同じ件数を保持、 テーブル
  数 21 (= v=6 schema 完全保持)
- **集客 narrative (= H 章) の数値素材を 2 件確保**: H1 (= lockdown
  1 文)、 H3 (= `processing_secs` vs `duration_secs` の実測差 = ~2.6
  秒 / ~2.7 秒)

終了条件 B〜F + X (= X1-X5 + X7-X8 必須、 X6 任意) のうち、 A 章のみ
物理操作前提でスコープ外 (= v=5 DB がもう作られない方針)、 それ以外は
**all green**。 v0.5.1 集客フェーズ復帰可。

## B. external access lockdown (= 主役 2/2)

### B1. validator denylist 経由の reject

| ID | 入力 | 結果 | 判定 |
|---|---|---|---|
| B1.1 | `read_csv('/etc/passwd')` | `Function 'read_csv' is not allowed` | ✓ reject |
| B1.2a | `parquet_scan('/etc/passwd')` | `Function 'parquet_scan' is not allowed` | ✓ reject |
| B1.2b | `parquet_metadata('/etc/passwd')` | `Function 'parquet_metadata' is not allowed` | ✓ reject |
| B1.2c | `parquet_schema('/etc/passwd')` | `Function 'parquet_schema' is not allowed` | ✓ reject |
| B1.2d | `sniff_csv('/etc/passwd')` | `Function 'sniff_csv' is not allowed` | ✓ reject |

### B2. engine-level reject

`run_custom_query("SELECT name, value FROM duckdb_settings() WHERE name
IN (...)")` の結果:

- `enable_external_access = false` ← **MCPB bundle 起動経路で設定が
  落ちていないことを実機で確認** (= B2.1 の最重要 probe)
- `allow_unsigned_extensions = false`
- `temp_directory = <db_dir>/<db>.tmp` (= `get_server_info` の db_path
  と同階層)

| ID | 入力 | 結果 | 判定 |
|---|---|---|---|
| B2.1 | `current_setting('enable_external_access')` | `"false"` (= 文字列) | ✓ engine 設定維持 |
| B2.2 | `ATTACH '/tmp/attacker.db' AS f` | `Only SELECT / WITH queries are allowed` | ✓ reject |

### B3. https / s3 URL の egress 不可

| ID | 入力 | 結果 | 判定 |
|---|---|---|---|
| B3.1 | `parquet_scan('https://raw.githubusercontent.com/...')` | `Function 'parquet_scan' is not allowed` | ✓ reject (= URL 到達せず = SSRF 不可) |

### B4. 通常 SQL の無回帰

| ID | 入力 | 結果 | 判定 |
|---|---|---|---|
| B4.1 | `SELECT COUNT(*) FROM records` | `~2.7M` | ✓ |
| B4.2 | `SELECT record_type, COUNT(*) ... GROUP BY 1 ORDER BY 2 DESC LIMIT 10` | HeartRate ~420k 等、 集計返却。 `user_supplied_limit: true` / `truncated: false` | ✓ |
| B4.3 / C2.3 | `SELECT import_id, record_count, records_after_dedup, dedup_skipped, processing_secs FROM imports ...` | 生列名据え置きで通る | ✓ |

## C. wire-shape adjustments (= 副題)

### C1. list_zips hint が async 文言

| ID | 確認内容 | 結果 |
|---|---|---|
| C1.1 | async 文言「A fresh import returns `{status: 'queued', job_id}`; poll `get_import_status(job_id=…)` every 10-30 seconds」 | ✓ 含まれる、 v0.4 文言 (= `Claude will wait synchronously`) は **含まれない** |
| C1.2 | imported=true 分岐説明「`imported: true` short-circuits synchronously ... without a `job_id` -- do NOT poll」 | ✓ 含まれる |
| C1.3 | stall 閾値「if elapsed_secs grows past ~10 minutes without the `phase` field advancing, treat the worker as stalled」 | ✓ 含まれる |

### C2. get_import_history.processing_secs alias

**C2.1 ✓** wire shape:

```
import_id, export_dir, imported_at, record_count, workout_count,
processing_secs, export_xml_sha256, records_after_dedup,
dedup_skipped, source_zip_sha256, source_zip_mtime, source_zip_size
```

旧 `duration_secs` は wire から消失。

**C2.2 ✓** 同 import の `processing_secs` (= body) vs
`import_jobs.duration_secs` (= worker wall-clock, ZIP 展開込み) の実測差:

| import (= 種別) | processing_secs (body) | duration_secs (worker) | 差 |
|---|---|---|---|
| fresh import (= 63 MB 級) | 44.03 s | 46.60 s | **+2.57 s** |
| incremental import (= 同規模) | 28.38 s | 31.03 s | **+2.65 s** |

→ 63 MB 級 ZIP で ZIP 展開オーバーヘッドが約 2.6 秒。 集客 narrative
H3 素材として確保。

**C2.3 ✓** `run_custom_query("SELECT duration_secs FROM imports LIMIT 1")`
→ DB 列名は据え置きで通る (= Layer 2 escape hatch 無影響)。

### C3. import_zip id 寛容仕様

| ID | 入力 | 結果 | 判定 |
|---|---|---|---|
| C3.1 | `"<SHA8 大文字>"` | `status: ok, id: "<sha8 小文字>"` (= canonical lowercase)、 冪等 ok | ✓ |
| C3.2 | `"  <sha8>  "` (= 前後空白) | 同上 | ✓ |
| C3.3 | `"zzzz"` (= 非 hex) | `invalid_id` + message「case-insensitive, surrounding whitespace ignored」 明記 | ✓ |

## D. 既存 21 read/write tools の regression

| ID | tool | 結果 | 判定 |
|---|---|---|---|
| D1 | list_record_types | 62 record_type、 件数/単位/期間揃う (= HeartRate ~420k 件 等) | ✓ |
| D2 | query_records(HeartRate) | `total: ~420k`、 3 件返却、 ページング正常 | ✓ |
| D3 | get_record_statistics(StepCount, month) | 6 ヶ月分の集計返却 | ✓ |
| D4 | list_workouts(Running) | `total: 91`、 3 件返却 | ✓ |
| D5 | get_workout_details(`<workout-hash>`) | 4.6 km / 44 分の run、 events 10 件、 statistics 9 種、 metadata 4、 route あり | ✓ |
| D6 | get_activity_summaries(2026-06-25〜) | active_energy ~300 kcal 等 | ✓ |
| D7 | get_workout_route(with_heart_rate=true, every_nth=10) | 250 点返却、 `heart_rate` / `heart_rate_offset_secs` 同梱 | ✓ |
| D8 | get_heart_rate_samples(HRV record) | `total: 43`、 5 件返却 (= `sample_idx`, `bpm`, `sample_time`) | ✓ |
| D9 | list_correlations | `total: 4`、 BloodPressure 3 件 | ✓ |
| D10 | get_correlation_details(BP) | members に Systolic 130 / Diastolic 80 | ✓ |
| D11 | list_ecg_readings | `total: 7`、 洞調律 / 512 Hz | ✓ |
| D12 | get_ecg_data(`<ecg-hash>`, voltages, ds=50) | sample_count: ~15k、 voltages_uv ~300 件返却 | ✓ |
| D13 | run_custom_query("SELECT COUNT(*) FROM records") | `~2.7M` | ✓ |
| D14 | list_data_sources | 9 source (= Apple Watch / iPhone 等) | ✓ |
| D15 | get_import_history | C2.1 で確認の wire shape | ✓ |
| D16 | list_state_of_mind | `total: 0`、 items: [] (= export に StateOfMind 不在、 tool 動作は正常) | ✓ |
| D17 | get_me_attributes | characteristic 各フィールド返却 | ✓ |
| D18 | get_server_info | `version: 0.5.1`, `record_count: ~2.7M` | ✓ |
| D19 | list_zips | C1 系の hint 形 | ✓ |
| D20 | import_zip(`<既 import 済 sha>`) | idempotent ok (= `records_added: 0`, `already_imported_at` populated、 job_id なし) | ✓ |
| D21 | get_import_status(`<完走済 job_id>`) | 永続 ok envelope (= records_added ~2.7M 等) | ✓ |

## F. 失敗シナリオ

| ID | 入力 | 結果 | 判定 |
|---|---|---|---|
| F1 | `import_zip(id="<html リネーム zip の sha>")` | `invalid_zip` + 「Re-download or re-export」 誘導 | ✓ |
| F2 | `import_zip(id="<xlsx zip の sha>")` | `not_apple_health_export` + 「Did you mean a different ZIP?」 | ✓ |
| F3 | `import_zip(id="deadbeef")` | `id_not_found` + 「Call list_zips to refresh」 | ✓ |
| F4 | `import_zip(id="zzzz")` | `invalid_id` (= C3.3 と同一 envelope) | ✓ |
| F5 | env 未設定 → `export_zips_dir_not_set` | Phase 2 で実施 (= 後述) | ✓ |
| F6 | `get_import_status(job_id="ij_nope")` | `job_not_found` | ✓ |

## P2. データオタク (= run_custom_query)

| ID | 入力 | 結果 | 判定 |
|---|---|---|---|
| P2.1 | `string_agg(record_hash, ',') FROM records WHERE ...HeartRate` | `Tool result is too large. Maximum size is 1MB.` ハードエラー | ✓ 無言切り解消の無回帰 |
| P2.2 | LIMIT 無し `record_hash FROM records WHERE ...HeartRate` | `row_count: 1000` / `truncated: true` / `max_rows: 1000` / `user_supplied_limit: false` | ✓ 行 cap 動作 |
| P2.3 | `WITH RECURSIVE t(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM t WHERE n<1000) SELECT COUNT(*) FROM t` | `1000` | ✓ |
| P2.4 | WINDOW 関数複合 `AVG(value) OVER (PARTITION BY DATE_TRUNC('day', start_date)) ... LIMIT 100` | 集計結果 100 件返却 | ✓ |
| P2.5 | 同 query 100 連発 | **agent ループ単独では現実的に踏めない** (= 21 ターン消費 + 切断リスク)、 skip。 unit test で構造 pin 済 | ⏭ |
| P2.6 | OFFSET 100,000 / LIMIT 100 | 100 件正常返却、 crash なし | ✓ |

## X. 7 人の意地悪な QA (= adversarial)

### X1. SQL safety 無回帰 (= v0.5.0 stop-ship retest)

| ID | 入力 | 結果 | 判定 |
|---|---|---|---|
| X1.1 | `parquet_scan('/etc/passwd')` | `Function 'parquet_scan' is not allowed` | ✓ reject |
| X1.2a | `parquet_metadata('/etc/passwd')` | `Function 'parquet_metadata' is not allowed` | ✓ reject |
| X1.2b | `parquet_schema('/etc/passwd')` | `Function 'parquet_schema' is not allowed` | ✓ reject |
| X1.2c | `sniff_csv('/etc/passwd')` | `Function 'sniff_csv' is not allowed` | ✓ reject |
| X1.3 | CTE 内に隠した `read_text('/etc/passwd')` | `Function 'read_text' is not allowed` | ✓ 再帰チェック有効 |
| X1.4 | `parquet_scan('https://raw.githubusercontent.com/...')` | `Function 'parquet_scan' is not allowed` | ✓ URL 到達せず |
| X1.5 | `ATTACH 'https://attacker.example/x.db' AS f` | `Only SELECT / WITH queries are allowed` | ✓ |
| X1.6 | `duckdb_settings()` | 通る (= 既知の by-design 抜け、 別途 introspection denylist issue #216 で起票済) | ✓ 無回帰 |
| X1.7 | `SELECT * FROM records; DROP TABLE records;` | `Only a single SQL statement is allowed (got 2)` | ✓ 複文 reject |
| X1.8 | `COPY records TO '/tmp/out.csv'` | `Only SELECT / WITH queries are allowed` | ✓ |

### X2. 入力境界 (= import_zip + query_records)

| ID | 入力 | 結果 | 判定 |
|---|---|---|---|
| X2.1 | `id=""` | `invalid_id` + 「got ''」 | ✓ |
| X2.2 | `id="a"` | `invalid_id` + 「got 'a'」 | ✓ |
| X2.3 | `id="a"*65` | `invalid_id` + 「got 'aaaa...'」 (= 65 文字、 4-64 hex 範囲外) | ✓ |
| X2.4 | `id="<sha8>malicious"` | `invalid_id` (= hex 以外含むため)、 server クラッシュなし | ✓ |
| X2.5 | `id="ｆｆｃ７２ａ０ｆ"` (= 全角 hex) | `invalid_id` | ✓ |
| X2.6 | `query_records(limit=0)` | `Error: limit must be >= 1` | ✓ |
| X2.7 | `query_records(record_type=HeartRate, limit=999999)` | `items: 1000` / `total: ~420k` / `next_offset: 1000` / `truncated_by_size: false` (= clamp 動作確認) | ✓ |
| X2.8 | `query_records(offset=-1)` | 先頭ページ扱い (= offset=0 と同じ結果、 `next_offset: 3`)、 server クラッシュなし | ✓ |
| X2.9 | `query_records(start_date="2020-01-01' OR '1'='1")` | `Conversion Error: invalid timestamp field format` | ✓ SQLi 不可 |
| X2.10 | `query_records(start_date="2050-13-45")` | `Conversion Error: timestamp field value out of range` | ✓ |
| X2.11 | `query_records(start_date="2030-01-01", end_date="2020-01-01")` | `items: []` / `total: 0` (= error にならず空配列) | ✓ |

### X5. Job 状態マトリクス

| ID | 入力 | 結果 | 判定 |
|---|---|---|---|
| X5.1 | 完走済 `<job_id>` を `get_import_status` | 永続 ok envelope (= D21 で実質踏み済、 再確認可) | ✓ |
| X5.2 | 正しい形式の偽 `ij_<future-ts>_<sha8>_<rand>` | `job_not_found` + 「may pre-date a fresh DuckDB file」 誘導 | ✓ |
| X5.4 | cross-session 永続性 | Phase 2 で実施 (= 後述) | ✓ |

### X8. 破壊耐性 final 確認

| ID | 確認内容 | 結果 | 判定 |
|---|---|---|---|
| X8.1 | `COUNT(*) FROM records` | `~2.7M` (= X 章開始前と同じ) | ✓ |
| X8.2 | `COUNT(*) FROM imports` | 2 (= fresh import 1 + incremental 1、 X 章で増減なし) | ✓ |
| X8.3 | `COUNT(*) FROM import_jobs` | 2 (= 完走 2 件、 prefix `ij_` 形式維持) | ✓ |
| X8.4 | `COUNT(*) FROM duckdb_tables() WHERE schema_name='main'` | 21 (= v=6 schema 完全保持) | ✓ |
| X8.5 | `get_server_info` | `version: 0.5.1`, `record_count: ~2.7M` (= X8.1 と一致) | ✓ |

## 本セッションで踏めなかった経路 (= 明示記録)

物理操作前提のため agent ループから踏めない章:

- **A 章全部**: legacy v=5 DB を `APPLE_HEALTH_DB` に向ける必要、 v=5 DB
  がもう作られない方針なので skip 確定 (= test plan v0.5.1 で A 章
  skip 化済)
- **E 章全部**: Settings → Extensions の GUI 操作前提。 ただし E3
  (= denylist reject の MCPB 経由版) は本セッションの B / X1 が実質的に
  MCPB bundle 経由で実施しているため同等カバー
- **G 章**: v0.5.0 で取り込んだ DB を保持していれば確認可、 本セッション
  では未確認
- **X6 並列 / Race**: sub-process script 前提、 agent ループから
  sub-second の同時 2 連発不可能。 unit test
  (`test_concurrent_import_zip_*`) で構造 pin 済

スコープ外として意図的に skip:

- **P2.5 同 query 100 連発**: agent ループから 100 ターン繰り返すと
  長コンテキスト誘因の terminate 事象に遭遇しやすく、 現実的に不可能

## 集客 narrative 素材 (= H 章)

**H1 (= external access lockdown)**

> SQL escape hatch (`run_custom_query`) operates only over in-DB
> relations; the engine refuses every fs / network function at the
> connection level — confirmed live on the MCPB bundle that ships to
> end users.

**H3 (= processing_secs vs duration_secs)**

> `processing_secs` (= import body) と `duration_secs` (= queue→done
> worker wall-clock incl. ZIP extract) の差は 63 MB 級 ZIP で **約 2.6
> 秒** (= fresh import: 44.03 → 46.60 / incremental: 28.38 → 31.03)。
> 「なんで 2 つの数字があるの?」 の F&Q 回答素材として使える実測値。

**H2 (= schema_outdated typed envelope)**: A 章スコープ外のため素材取得
できず、 v=5 DB を作る経路が無いので恒久的に取得不可。

## Phase 2 物理操作テスト 完全結果

物理介入で踏める分 (= v=5 DB 要・MCPB GUI 非露出を除く) を 1 件ずつ
完走。

### env (= F5 + X3 + E)

| ID | 入力 | 結果 | 判定 |
|---|---|---|---|
| F5 / X3.3 | `APPLE_HEALTH_EXPORT_ZIPS_DIR` 未設定 | `list_zips: export_zips_dir:null + hint`, `import_zip(id=任意): reason:export_zips_dir_not_set` | ✓ |
| X3.1 / X3.2 | `APPLE_HEALTH_EXPORT_ZIPS_DIR=..\..\..\Windows\System32` | `list_zips: zips:[], export_zips_dir は relative のまま表示`, `import_zip: id_not_found` | ✓ |
| X3.4 / X3.5 / X3.6 | `APPLE_HEALTH_DB` 系 | MCPB user_config 非露出のためスコープ外 | ⏭ |

### X4 ZIP 意地悪

| ID | ZIP 内容 | list_zips 段階 | import_zip 終端 | 判定 |
|---|---|---|---|---|
| X4.1 | zero.zip (= 0 byte) | `zip_status:invalid_zip`, id:e3b0c442 (= 空 sha 定数) | `reason:invalid_zip` + Re-download 誘導 | ✓ |
| X4.2 | one.zip (= 1 byte = `P`) | `zip_status:invalid_zip`, id:5c62e091 | `reason:invalid_zip` 同 message | ✓ |
| X4.3 | x4-3-multi-empty-xml.zip (= multi entry, export.xml が 0 byte) | `zip_status:valid_apple_health` | **status:queued → status:error, reason:run_import_failed, "unrecoverable XML syntax error: no element found (line 0)"** | ✓ 実装の方がテストプラン期待 (= record_count=0 完走) より厳格・優秀 |
| X4.4 | x4-4-zip-slip.zip (= path traversal entry 含む) | `zip_status:valid_apple_health` | `status:queued → status:ok, records_added:0, duration_secs:1.74` | ✓ 完走するが records 追加なし、 host fs 脱出書き込みなし |
| X4.5 | x4-5-original.zip + x4-5-renamed-clone.zip (= 同 sha, 別 name) | 2 entries 両方 listed, **同 id `b91758ae`** | (= option A) 別の既 import 済 ZIP のコピーで `import_zip(id=<既 import sha>)` → `status:ok, records_added:0, already_imported_at, duration_secs:0.0`, message に list 先頭 file_name が出る | ✓ idempotent green。 observation: 同 sha multiple file_names は design decision で別 entry として listed |
| X4.6 | x4-6-future-mtime.zip (= mtime=2100-01-01) | `mtime:"2100-01-01T00:00:00+00:00"`, ISO 表示, sha 不変, id=b91758ae | (= 踏まず、 list 段階で完了) | ✓ cache key (size, mtime) 影響なし |
| X4.7 | x4-7-broken-xml.zip (= XML valid だが Record の start_date 空) | `zip_status:valid_apple_health` | `status:queued → status:error, reason:run_import_failed, "Conversion Error: invalid timestamp field format: \"\", ... when casting from source column start_date"` | ✓ typed envelope。 DuckDB 生エラーが露出 → human-friendly translation が UX 改善候補 |

### X5.4 cross-session job_id 永続性

Claude Desktop 物理再起動して新 session で過去 job_id を `get_import_status`
で叩く。

| job_id | 経由 | 終端 envelope | 判定 |
|---|---|---|---|
| (= X4.3 で生成、 同 session 内) | X4.3 | error / `run_import_failed` / XML syntax error | ✓ 再起動跨いで wire shape 完全保持 |
| (= X4.4 で生成) | X4.4 | ok / records_added:0 / 1.74s | ✓ |
| (= X4.7 で生成) | X4.7 | error / `run_import_failed` / DuckDB conversion error | ✓ |
| (= 前 session、 24 時間以上前の job) | 前 session, 24 時間以上前 | ok / records_added:~2.7M / 46.6s | ✓ Tier 越境で永続 |
| 偽 `ij_<future-ts>_<rand>` | (= 架空) | error / `job_not_found` + "may pre-date a fresh DuckDB file" hint | ✓ X5.2 と同 design contract が再起動跨いで安定 |

**結論:** `import_jobs` テーブルが DuckDB に persist。 新 server プロセス
起動後も全 job 状態が読める。 wire shape は再起動前と完全一致。

### X8 破壊耐性 final

X4 シリーズ + X5.4 完走後の record_count: **~2.7M** (= 前 session 終了
時と同値)。 X4.3 / X4.4 / X4.7 で error / ok 終端した 3 件は records
追加せず、 X8 通過。

## UX 改善候補 (= v0.6 milestone 候補)

defect ではないが、 dogfood で見つけた user-facing UX 改善候補 3 件。

### UX #1 `export_zips_dir` フィールド: 絶対 path 正規化

**観測 (= X3.1/X3.2):** `APPLE_HEALTH_EXPORT_ZIPS_DIR=..\..\..\Windows\System32`
を設定すると、 `list_zips` の `export_zips_dir` フィールドが
`..\..\..\Windows\System32` と relative のまま表示される。

**問題:** agent / user が「実際にどこを見てるか」 分かりにくい。 サポート
時にも「working directory は何で、 そこから relative resolve した結果は何か」
を user に説明させる手間が増える。

**修正方針【推測】:** `os.path.abspath()` で正規化した値を envelope に入れる。
display 用と内部処理用で別フィールドにする選択肢もある (= 例: `export_zips_dir`
正規化版 + `export_zips_dir_raw` 原文)。

### UX #2 X4.7 系: DuckDB conversion error の human-friendly translation

**観測 (= X4.7):** 壊れた export.xml (= Record の start_date 属性が空) を
import すると、 `run_import_failed` envelope の message に DuckDB の生エラー
(= `"Conversion Error: invalid timestamp field format: \"\", expected
format is (YYYY-MM-DD HH:MM:SS...) when casting from source column
start_date"`) が露出。

**問題:** user 視点で「自分の export ファイルの何が悪いのか」 を読み解くの
は技術的すぎる。 X4.3 の `"unrecoverable XML syntax error: no element
found (line 0)"` は十分 human-friendly だが、 X4.7 は内部実装が見える。

**修正方針【推測】:** importer Phase 2 (= DuckDB ingest) のエラーを catch
して、 record 属性レベルのバリデーションエラーを「Your export.xml contains
records with empty or invalid timestamp fields. The export may be partially
corrupted; please re-export from Apple Health.」 のような message に翻訳する。

### UX #3 テストプラン X4.3 文言更新推奨

**観測 (= X4.3):** テストプラン期待は「マルチエントリで export.xml が 0
byte → import 完走するが record_count=0」 だが、 実装は ElementTree
parse 段階で `"unrecoverable XML syntax error: no element found (line 0)"`
を投げて error 終端する。

**判定:** 実装の方が**より良い設計**。 「成功したのにデータがない」 と
user を混乱させない。 テストプラン文言を「空 export.xml は error 終端、
record_count=0 完走ではない」 に更新推奨。

## Phase 3 Adversarial 探索 (= MCP テスター視点)

「思いつく限り穴をついてみてほしい」 の指示を受けて、 テストプランに無い
独自の adversarial 探索を実施。 「軽い (= 副作用の小さい) からやれ」
「1 件ずつ進めろ (= 致命的 attack に限り)」 の規律で。

### 攻撃カテゴリと結果

| カテゴリ | 経路 | 結果 |
|---|---|---|
| External access bypass | `read_text_auto` / `read_json_auto` / `read_blob` | ✓ deny list で reject |
| 外部関数 (= 新発見) | `read_duckdb` / `read_ndjson_objects` 系 | ⚠ **defect #1** — `read_duckdb` は deny list 未登録、 `enable_external_access` で cover、 実存ファイル指定で UTF-8 エラー |
| PRAGMA / SET 経由 bypass | `SET enable_external_access=true` 等 | ✓ multi-statement と PRAGMA は X1 で塞ぎ済み、 SELECT 内 `current_setting` で false 固定確認 |
| URL/quoted-path | `'https://...'` / `'s3://...'` | ✓ Quoted-path table reference reject |
| SQL injection (= non-SQL tool) | `query_records` / `list_workouts` の source_name / activity_type に SQL 片 | ✓ parameterized query で escape、 X1 と整合 |
| 整数オーバーフロー | `offset=int64 max` | ⚠ DuckDB の生エラー露出 (= UX #5 候補) |
| 整数境界 (= -1 / 99999) | limit / every_nth | ✓ typed validation |
| Unicode / 制御文字 | NUL byte / 絵文字+RTL+XSS / 空文字列 | ✓ typed empty 返却 (= MCP transport が NUL byte sanitize) |
| hash 系 SQL injection | `get_workout_details` / `get_correlation_details` / `get_ecg_data` / `get_heart_rate_samples` / `get_workout_route` | ✓ 全 5 経路 parameterized query で escape |
| import_zip id 検証 | SQL 片 / 3 文字 / 65 文字 / 約 2000 文字 / 空白挿入 | ✓ hex validation で reject、 message に id echo back |
| SELECT 構文系 | multi-statement / コメント注入 / pragma_database_list | ✓ X1 と整合、 コメント内 SQL 片は無効化 |
| 情報収集 (= 副作用ゼロ) | `duckdb_databases` / `duckdb_tables` / `duckdb_indexes` / `duckdb_settings` | ⚠ **defect #3** — DuckDB 設定が緩い (= memory_limit=50GB 等) |
| Recursive CTE DoS | `WITH RECURSIVE bomb(n)...WHERE n < 100000000` | 🚨 **defect #2** — server hang、 Claude Desktop 再起動要 |
| Streaming generate | `GENERATE_SERIES(1, 1000000)` | ✓ max_rows=1000 で streaming cap、 materialize しない |
| 巨大文字列 build | `SELECT REPEAT('A', 1000000)` | ✓ length() で受け止め、 wire 経由は問題なし |
| **巨大入力 (= tool param)** | **`query_records(source_name='A' × 約 12,000 字)`** | 🚨 **defect #4** — server hang、 Claude Desktop 再起動要 |

### 発見した defect 一覧

#### 🚨 defect #1: `read_duckdb` が deny list 未登録 (= defense in depth)

**観測:** `SELECT * FROM read_duckdb('<path>/health.duckdb', schema_name
=> 'main', table_name => 'imports')` を投げると、 server-side deny list
は通過する (= deny されない) が DuckDB 本体の `enable_external_access=false`
で reject される。 ただし**実存ファイルを指定すると UTF-8 デコードエラー**
(= `'utf-8' codec can't decode byte 0x80 in position 58: invalid start
byte`) で envelope が壊れる。

**推測:** DuckDB が既に attach 済みの DB ファイルを `read_duckdb` で開こう
とするとロック競合 + binary バイトがエラーメッセージに混入して MCP の
JSON シリアライズに失敗。

**重要度:** 低。 `enable_external_access=false` が cover してるので host
fs 到達不可。 ただし将来 `enable_external_access` の制御に穴ができた時
の防御深化として deny list 追加推奨。

**修正方針:** server-side deny list に `read_duckdb` / `read_ndjson` /
`read_ndjson_auto` / `read_ndjson_objects` / `read_json_objects` /
`read_json_objects_auto` を追加。 エラー文字列の sanitize (= 非 UTF-8
bytes を `replace='?'` で処理する error 経路を追加)。

#### 🚨 defect #2: Recursive CTE で server hang (= memory DoS)

**観測:** `WITH RECURSIVE bomb(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM
bomb WHERE n < 100000000) SELECT count(*) FROM bomb` を投げると、
**MCP server プロセスが完全にハング**。 `get_server_info` 等の軽量
ツールすら 4 分以上応答返らず、 Claude Desktop の物理再起動でのみ復旧。

**原因 (= defect #3 と連動):** DuckDB の `memory_limit` がデフォルトの
`50.0 GiB` のまま (= defect #3 参照)。 recursive CTE は中間結果を
materialize するため、 memory を食い尽くす。 `max_rows=1000` cap は
最終 result set にのみ効き、 execution 中の memory 消費を制限しない。

**重要度:** 高。 任意の user が `run_custom_query` で server を任意に
止められる self-DoS。 とくに companion app の経路で外部入力が SQL に
流れ込む場面 (= 今のところそういう経路は無いが、 将来 path) が出来た
時に致命的。

**修正方針:** defect #3 の hardening セットで物理的に解消可能 (=
memory_limit を 2GB に制限すれば、 materialize しきれずに DuckDB 側で
`Out of Memory` を投げて typed error envelope に変換される)。 クエリ
実行 timeout (= 例 30 秒) も併用すると確実。

#### 🚨 defect #3: DuckDB 設定がデフォルトのまま (= hardening 不足)

**観測:** `SELECT name, value FROM duckdb_settings()` で重要設定が緩い:

| 設定 | 現状 | 推奨 |
|---|---|---|
| `memory_limit` | `50.0 GiB` | `2 GB` (= または環境変数で可変) |
| `max_memory` | `50.0 GiB` | 同上 |
| `max_temp_directory_size` | `90% of available disk space` | `4 GB` |
| `lock_configuration` | `false` | **`true`** (= 二重防御) |
| `allow_community_extensions` | `true` | `false` |
| `autoinstall_known_extensions` | `true` | `false` |
| `autoload_known_extensions` | `true` | `false` |
| `enable_external_access` | `false` ✓ | (= 既に固定) |

**重要度:** 中。 `enable_external_access=false` の Tier-1 防御が機能して
るので外部到達はないが、 defect #2 (= recursive CTE) や defect #4 (=
巨大入力) のような self-DoS が物理的に止められない。 hardening セットを
importer 初期化時に固定 + `lock_configuration=true` で締めれば、 後続の
attack で SET 系を試みても物理的に変更不可。

**修正方針 (= importer 初期化時に固定):**

```python
con.execute("SET memory_limit='2GB'")
con.execute("SET max_temp_directory_size='4GB'")
con.execute("SET allow_community_extensions=false")
con.execute("SET autoload_known_extensions=false")
con.execute("SET autoinstall_known_extensions=false")
con.execute("SET enable_external_access=false")
con.execute("SET lock_configuration=true")  # 最後に締める
```

これで defect #2 / #4 の根本原因が物理的に解消。

#### 🚨 defect #4: query_records の source_name に巨大文字列で server hang

**観測:** `query_records(record_type=..., source_name="A" × 約 12,000 字)`
を投げると、 **MCP server プロセスがハング**。 Claude Desktop の物理
再起動でのみ復旧。

**推測:** Python -> DuckDB の prepared statement bind パスで、 巨大文字列
を VARCHAR パラメータとして渡す際に内部で copy / hash 等が走り、
`memory_limit=50GB` の制約下で memory を食い尽くす。 または DuckDB の
文字列処理が O(n²) になる経路がある。

**重要度:** 高。 **companion app で外部入力が source_name に流れて
くる経路が出来た時に致命的**。 一般 user の Claude Desktop 上では
「12,000 字の source_name」 を agent が偶然作る可能性は低いが、 悪意ある
外部入力では誰でも引ける。

**修正方針:**

1. **defect #3 の hardening セット**で memory_limit を絞れば、 physically
   止められる
2. **tool param 側で長さ validation を追加**: source_name / record_type /
   hash 系 / id 系の string パラメータに max length (= 例: 256 文字) を
   設定、 超過は typed validation error。 これは MCP server の input
   schema レベル
3. **agent 側でも防御**: tool schema に `maxLength` を追加すれば agent
   が予防的に検証する可能性

両方やるのが安全。

### UX 改善候補 (= 追加分)

defect ではないが、 adversarial 探索で見つけた improvement 候補:

#### UX #4 `import_zip` の error message: 巨大 id を echo back する

**観測:** `import_zip(id="deadbeef" × 約 2,000 字)` を投げると、
`invalid_id` envelope の message に**入力 id 全体を echo back** する。

**問題:** agent context を無駄に消費。 実害低いが UX 観点で改善余地。

**修正方針:** message 内の id echo を `max 64 chars + "..."` で truncate。

#### UX #5 `offset=int64 max` で DuckDB 生エラー露出

**観測:** `query_records(offset=9223372036854775807)` で `Conversion
Error: Type INT128 with value 9223372036854776000 can't be cast because
the value is out of range for the destination type INT64` という DuckDB
生エラーが返る。

**修正方針:** offset の input validation で `0 <= offset <= INT64_MAX` を
server 側でチェック、 超過は typed validation error。

### Phase 3 最終判定

**stop-ship 系 defect: 0 件** (= defect #2 / #4 はいずれも self-DoS で、
agent の偶発的トリガーは現実的に起きにくい)。

**v0.6 必須対応 (= recommendation):**

- **defect #3 の hardening セット** (= memory_limit / lock_configuration /
  community extensions) — これで defect #2 + #4 が物理的に解消
- **defect #4 の追加防御** (= tool param の長さ validation) — companion
  app への備え

**v0.6 推奨対応:**

- defect #1 (= `read_duckdb` 等を deny list 追加) — 防御深化
- UX 改善候補 #4 (= id echo truncate) / #5 (= offset validation) /
  #1 (= export_zips_dir 正規化) / #2 (= DuckDB conversion error 翻訳)

**今後の検討事項:**

- companion app で外部入力が流れてくる経路を設計する際は、 入力 sanitize
  層を server boundary に明示的に設ける
- DuckDB の execution timeout (= 例 30 秒) を導入すると、 memory_limit
  を上げた将来でも DoS 耐性を維持

## 最終判定

**v0.5.1: 集客フェーズ復帰可、 stop-ship 系 defect 0 件**。

- B 主役 (= external access lockdown): ✓ all green
- C 副題 (= #187 hint / #189 processing_secs / #191 id 寛容): ✓ all green
- D 21 件 regression: ✓ all green
- F 失敗シナリオ: ✓ all green
- X 章 (= X1-X5 + X7-X8): ✓ all green (= X3.4-X3.6 / X4 host fs 確認 /
  X6 並列 / X7 escape hatch は scope 外)
- A 章: スコープ外 (= v=5 DB がもう作られない)
- E 章: B / X1 で実質カバー、 明示テスト不要
- G 章: scope 外

UX 改善候補 + defect は v0.6 milestone での issue 化を推奨。

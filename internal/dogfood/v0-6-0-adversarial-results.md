# v0.6.0 Adversarial Review 結果

- 対象: `apple-health-mcp-server` v0.6.0（アップロード zip のソース + ライブ MCP サーバー）
- 実施日: 2026-07-02
- ライブ DB: 実データ import 済み（records 2,702,294 件 / v=6 スキーマ）
- 方針: `adversarial-attack-log.md` の既出 attack vector（#1〜#103）は再試行せず、**カタログに無い新角度**のみを狙う。各 attack は「入力（verbatim）/ 観測結果（verbatim）/ 判定 / 既出への類似度」で記録。

---

## 実装担当（implementer）への指示

このドキュメント単体で着手できるよう指示を明記する。**N1・N2 を修正対象とする。最小差分・目的外は触らない。**

- **N1 修正**: `src/apple_health_mcp/server/safety.py` の `DENIED_FUNCTIONS` に `current_setting` を追加する。合わせて N3 の `duckdb_temporary_files`（および path を吐く `duckdb_memory`）も同 PR で追加する。将来 read tool が `current_setting('TimeZone')` 等を内部利用する設計が入る場合は全面 deny ではなく「引数 name が `*_directory` / `*_path` / `home_directory` なら reject」の引数ベース判定に切り替える（詳細は N1 セクション）。既存の `tests/unit/server/test_safety.py` の denylist parametrize に自動で乗るはずだが、`current_setting('temp_directory')` が parse-time reject される pin を明示追加すること。
- **N2 修正**: `run_custom_query` 実行経路（`server/tools/run_custom_query.py` → `server/query.py` の `_execute`）に wall-clock timeout を導入し、期限超過で別スレッドから `conn.interrupt()` を叩く。`import_zip` の非同期 worker パターンを流用可能。DuckDB の pinned 版に SET 可能な statement timeout があるか先に確認し、あればそちらを優先（`_set_engine_safety_pragmas` に 1 行追加）。無ければ app 層 interrupt。共有 lock の道連れ緩和（別接続への退避）は設計判断を伴うため本 PR のスコープ外、別 issue とする。
- **N6 修正**: `server/query.py` の `clip_items_to_size_budget` の per-item 見積りがネストインデントを勘定していない（トップレベル `json.dumps(item, indent=2)` で測るが、実 payload では item が `items` 配列内に 2 段深くネストされ、+約 24% 過小評価）。堅牢版は「payload 全体を 1 回 serialize して実バイトを測り、超過なら item を落として再測（二分探索でも可）」。応急版は `DEFAULT_SIZE_BUDGET_BYTES` を実測過小評価率を吸収する値（例: 750KB）に下げる。既存 pin `test_run_custom_query_caps_...` とは別に、「clip が truncated_by_size=True を返した payload の実 serialize バイトが 1MB 未満」を保証する回帰テストを追加すること（本レビューの standalone 再現スクリプトのロジックがそのまま流用可能）。
- **N8 修正**: `server/safety.py` の `validate_query` の parse ガードを `except (sqlglot.errors.ParseError, RecursionError) as exc:` に拡張し、`QueryValidationError` へ変換する（深いネスト SQL で sqlglot が builtin `RecursionError` を投げ、現状 `run_custom_query` の `except QueryValidationError` も素通りして FastMCP 層で未処理例外化する）。`tests/unit/server/test_safety.py` に「深いネスト入力が typed reject される」pin を追加。余力があれば parse 前にネスト深度/長さのヒューリスティック reject も検討（sqlglot が他の builtin 例外を投げる病的入力への保険）。

### 環境依存の但し書き（implementer 側で再現できない項目）

implementer はソース修正担当であり、依頼者のライブ Windows 環境 / 実 DB を叩かない。以下は**本レビューで機構確定済みだが、implementer 側では再現できない**ため、再現テストではなく修正の妥当性検証（unit test / code review）で担保すること。

- **N1 のリーク実証出力**（`C:\Users\<user>\...` の path・username）は依頼者のライブ DB 観測値。implementer 側は「`current_setting('temp_directory')` が validator を通過して実行される」ことを unit test で pin すれば足りる（実際の path 値は環境依存で不問）。
- **N2 の全件 range join 実測**（7.3 兆比較が何分で走り続けるか）は未実施。機構は小規模 probe（9M 比較即時）+ コードレビュー（timeout / interrupt 皆無）で確定済み。implementer は「timeout 機構の追加」を実装すればよく、全件版の再現は不要。

---

## サマリ（先出し）

| # | 攻撃 | 判定 | 既出との差分 |
|---|---|---|---|
| N1 | `current_setting('temp_directory')` 等での内部 path / username リーク | 🚨 defect 候補 | #17（`duckdb_settings()` table-func）と同目的・**別アクセサ**。v0.6 #216 の denylist が scalar 形を取りこぼし |
| N2 | `run_custom_query` の execution timeout / interrupt 不在（CPU 律速 self-DoS + 共有 lock 道連れ） | 🚨 defect 候補 | #100（recursive CTE = memory 枯渇）と**別軸**。v0.6 #222/#223 hardening は memory 系のみ塞いだ |
| N3 | `duckdb_temporary_files()` / `duckdb_memory()` が validator 通過 | ⚠ 副次 gap | N1 と同根。`temporary_files` は spill 中 temp path 露出、`memory` は memory 統計のみ（path/PII なし） |
| N4 | DuckDB FROM-first 構文の validator 分類 | ✅ block 不要（正常 read） | parser differential 系の新規確認、defect なし |
| N5 | `get_record_statistics(period=...)` への SQL 片注入 | ✅ block | カタログは query_records 偏重。本 tool の period 経路は未記載だったが whitelist + no-echo で堅牢 |
| N6 | envelope 系全 tool の size-budget clamp がネストインデント分を過小評価 → 生 1MB エラー漏れ | 🚨 defect 候補 | 未対応欄「巨大 nested JSON transport」の初実測。#171 の graceful degradation 契約が破れる |
| N8 | 深いネスト SQL で `RecursionError` が validate_query の catch を素通り → 未処理例外 | 🚨 defect 候補 | 未対応欄「巨大 SQL parser DoS」の初実測。hang ではなく typed-error 契約の破れ |
| N7 | `list_workouts` の limit 上限 | ✅（撤回済み）| 当初「上限 clamp 無し」と疑ったが `_MAX_LIMIT=500` で clamp 済み。description に max 未記載の doc 漏れのみ（軽微）|
| — | engine hardening（memory_limit / external_access / lock_configuration / community_ext）ライブ確認 | ✅ 稼働確認 | #29 で「default のまま」と指摘 → v0.6 で適用済みを実測確認 |

**stop-ship 級**: N1（PII リーク）と N2（no-timeout self-DoS）。N6・N8 は robustness / 契約破れ（severity 中）。いずれも companion app で外部入力経路ができた時に深刻度が上がる。**severity は暫定、最終判定は依頼者の領分。**

---

## N1 🚨 `current_setting()` scalar 経由の内部 path / Windows username リーク

### 入力
```sql
SELECT current_setting('temp_directory') AS tmp,
       current_setting('memory_limit')   AS mem,
       current_setting('enable_external_access') AS ext,
       current_setting('lock_configuration')     AS lock
```

### 観測結果（verbatim）
```json
{
  "rows": [
    {
      "tmp": "C:\\Users\\<user>\\AppData\\Local\\Packages\\Claude_pzs8sxrjxfjjc\\LocalCache\\Local\\apple-health-mcp\\health.duckdb.tmp",
      "mem": "1.8 GiB",
      "ext": false,
      "lock": true
    }
  ],
  "row_count": 1, "truncated": false, "max_rows": 1000, "user_supplied_limit": false
}
```

追加確認（別 path 系設定も同様に抜ける）:
```sql
SELECT current_setting('extension_directory') AS ext_dir,
       current_setting('secret_directory')    AS secret_dir,
       current_setting('home_directory')       AS home_dir,
       current_setting('allow_community_extensions') AS community_ext
```
```json
{"rows":[{"ext_dir":"","secret_dir":"C:\\Users\\<user>\\.duckdb\\stored_secrets","home_dir":"","community_ext":false}]}
```

対比（v0.6 #216 で denylist 追加した table-func 側は塞がっている）:
```sql
SELECT value FROM duckdb_settings() WHERE name = 'temp_directory'
```
```
Error: Function 'duckdb_settings' is not allowed (reads host files or external resources)
```

### 判定: 🚨 defect 候補

- リーク内容: host の完全 path、および **Windows username `<user>`**（`C:\Users\<user>\...`）。`secret_directory` も同 username を含む path を露出。
- 根本原因: v0.6 #216 は `duckdb_settings` / `duckdb_databases` / `duckdb_extensions` を `DENIED_FUNCTIONS` に追加したが、**同じ session 設定を返す scalar アクセサ `current_setting()` を denylist に入れていない**。table-func 形だけ塞ぎ、scalar 形を取りこぼした典型的な enumeration 不完全。
- `enable_external_access=false` は生きているため fs 脱出・data exfil はできない。影響は **info leak（PII + 内部 path）** に限定。ただしプロジェクトの「データは完全ローカル・外部送信なし」というプライバシー契約の観点では、username / path が LLM の context（= 場合により外部モデル）に渡る点が契約と衝突しうる。

### 影響 + 推奨修正【推測】

- 影響: 単体では低〜中（RCE でも data exfil でもない）。ただし #216 の修正意図（path リーク遮断）を **scalar 経路で完全に回避できる**ため、fix が実質未完である点が問題。
- 推奨修正【推測】: `current_setting` を `DENIED_FUNCTIONS` に追加するのが最小差分。エージェントが `current_setting` を正当に必要とする read ユースケースは無いはず（TZ 等の設定はサーバー側 env で決まる）。**外れる可能性**: もし将来 `current_setting('TimeZone')` 等を read tool が内部利用する設計が入ると全面 deny では困る。その場合は「引数 name が path 系設定（`*_directory` / `*_path` / `home_directory`）なら reject」の引数ベース判定に切り替える。
- Test Basis: `safety.py` の `DENIED_FUNCTIONS` コメント「v0.6 #216: in-DB introspection functions. These are NOT covered by `enable_external_access = false` ... this denylist is the ONLY guard closing this leak」。この設計意図に対し `current_setting` が穴になっている。

### 既出への類似度

- #17（`duckdb_settings()` が temp_directory リーク → by-design → v0.6 で deny）と**同目的・別アクセサ**。#26（`current_setting` を enable_external_access の false 確認に使用）は同関数を触っているが、**path リーク vector としては未記載**。よって新規。

---

## N2 🚨 `run_custom_query` に execution timeout / interrupt watchdog が無い（CPU 律速 self-DoS + 共有 lock 道連れ）

### 入力（機構確認用の小規模 probe。本番の全件版は意図的に撃っていない。理由は後述）
```sql
SELECT count(*) AS n
FROM (SELECT value FROM records WHERE value IS NOT NULL LIMIT 3000) a,
     (SELECT value FROM records WHERE value IS NOT NULL LIMIT 3000) b
WHERE a.value > b.value
```

### 観測結果（verbatim）
```json
{"rows":[{"n":3665552}],"row_count":1,"truncated":false,"max_rows":1000,"user_supplied_limit":false}
```
3,000 × 3,000 = 9M 比較が即時（体感 <1s）で返却。

### コードレビュー（timeout / interrupt の不在確認）
`grep -rniE "timeout|interrupt|watchdog|cancel|query_timeout"` の結果、query 実行経路にヒット無し。timeout 参照は全て `import_zip` の MCP tool-call timeout 対策（= それが非同期 worker 化の理由）のみ。実行経路は:

```
run_custom_query → query_to_json(conn, sql, lock=lock) → _execute → conn.execute(sql); cursor.fetchall()
```
`query.py`:
```python
def query_to_json(conn, sql, params=(), *, lock=None):
    if lock is None:
        return _execute(conn, sql, params)
    with lock:                       # ← 実行中ずっと共有 lock を保持
        return _execute(conn, sql, params)
```
`conn.execute` を割り込む機構（`conn.interrupt()` を叩く watchdog 等）がどこにも存在しない。

### 判定: 🚨 defect 候補

- 外挿: 小規模 probe が示す通り range join 自体は高速。全件版 `records × records` は 2,702,294² ≈ **7.3 兆比較**。`count(*)` は streaming 評価で **メモリを消費しない**ため `memory_limit=2GB`（v0.6 hardening）では止まらず、timeout も無いため**数時間〜日単位で走り続ける**。
- 悪化要因: `run_custom_query` は実行中 **共有 `lock` を保持し続ける**。全 read tool（query_records / get_record_statistics 等）は同じ `conn` + `lock` を共有するため、暴走クエリ 1 本で **サーバーの read 面全体がブロック**する（単なる self-DoS ではなく whole-server wedge）。
- #100（recursive CTE）は memory 枯渇で hang したが、v0.6 #222/#223 の `memory_limit=2GB` + `max_temp_directory_size=4GB` で fail-fast 化された（= memory 軸は塞がれた）。本件は **CPU 律速・低メモリ軸**であり、hardening の射程外。

### 影響 + 推奨修正【推測】

- 影響: サーバープロセスの read 面が長時間停止。attack-log の戒め通り、Claude Desktop 側で待ちきれず物理再起動 → セッション進捗の巻き戻りに直結。
- 推奨修正【推測】:
  1. `run_custom_query` を worker thread + wall-clock timeout で回し、期限超過で別スレッドから `conn.interrupt()` を叩く（`import_zip` の非同期 worker パターンを流用可能）。DuckDB の `interrupt()` は別スレッドから安全に呼べる。
  2. または pinned DuckDB 版に statement-level timeout（`SET`可能なもの）があればそれで 1 行対応。**外れる可能性**: 使用中の DuckDB 版に該当 SET が無い可能性が高い（`_set_engine_safety_pragmas` にも設定していない）ため、要バージョン確認。無ければ (1) が現実的。
  3. lock 道連れの緩和として、`run_custom_query` のみ別 read-only 接続に逃がす案もあるが、`read_only=True` open は同一プロセス同一ファイル制約に触れるため設計判断が要る（スコープ外、別 issue 推奨）。
- Test Basis: `connection.py` の `_set_engine_safety_pragmas`（memory / temp / external_access のみ、timeout 無し）、`query.py` の `_execute`（interrupt 機構無し）。attack-log 未対応欄「DuckDB execution timeout の不在」に対応する初の実機構確認。

### なぜ本番の全件版を撃たなかったか

attack-log の「致命的リスク attack は単独実行」規律に従い、**7.3 兆比較の全件版は撃っていない**。撃つと tool call が数時間ブロックし、Desktop 物理再起動 → 本セッションで発掘した N1 含む全結果が巻き戻る。小規模 probe（9M 比較）+ コードレビュー（timeout / interrupt 皆無）で機構は十分確定したと判断。全件版での「実際に何分で OOM 化 or 走り続けるか」の実測が要るなら、他の発掘結果を保全した状態で単独セッションで実施を推奨。

### 既出への類似度

- #100（`WITH RECURSIVE` で memory 枯渇 hang）と**別軸**。#100 は materialize による memory 枯渇、本件は低メモリ CPU 律速。#101（`GENERATE_SERIES` は streaming で safe）とも別（あちらは max_rows cap が効く単一列生成、本件は集約で cap が効かない）。よって新規。

---

## N3 ⚠ `duckdb_temporary_files()` が validator を通過（N1 副次）

### 入力 / 観測結果
```sql
SELECT * FROM duckdb_temporary_files()
```
```json
{"rows":[],"row_count":0,"truncated":false,"max_rows":1000,"user_supplied_limit":false}
```

### 判定: ⚠ by-design 通過（denylist 未登録）

- `DENIED_FUNCTIONS` 未登録のため validator を通過。現状は spill 中のファイルが無く空返し。ただし N2 のような spill を伴うクエリ実行中には **temp file path（= username 込み）を露出**しうる。N1 と同じ「introspection 系 path リーク」クラス。
- 推奨: N1 の fix（`current_setting` deny）と同じ PR で `duckdb_temporary_files` / `duckdb_memory` も denylist へ。#28 で `duckdb_databases/tables/indexes` を「by-design」判定したまま、v0.6 では `duckdb_databases` のみ deny 追加 → `duckdb_tables` / `duckdb_indexes` / `duckdb_temporary_files` / `duckdb_memory` が取り残されている（path を吐くのは temporary_files / memory 系）。

### 既出への類似度
- #28（`duckdb_databases/tables/indexes` を by-design 判定）の残件。v0.6 が一部のみ deny したため gap が細分化された。

---

## N4 ✅ DuckDB FROM-first 構文の validator 分類（parser differential 確認）

### 入力 / 観測結果
```sql
FROM records SELECT count(*) AS n
```
```json
{"rows":[{"n":2702294}],"row_count":1,"truncated":false,"max_rows":1000,"user_supplied_limit":false}
```

### 判定: ✅ defect なし
- sqlglot が DuckDB FROM-first 構文を `exp.Query` として正しく分類し、validator を通過 → 正常な read として実行。DDL/DML 誤分類も fs 到達も無し。parser differential 経由の validator バイパスは（この構文では）成立しない。

---

## N5 ✅ `get_record_statistics(period=...)` への SQL 片注入

### 入力 / 観測結果
```
get_record_statistics(record_type="HKQuantityTypeIdentifierHeartRate",
                      period="week')); DROP TABLE records;-- ")
```
```
Error: invalid period; accepted values: day, month, week, year
```

### 判定: ✅ block
- `period` は `_PERIOD_TRUNCS` の whitelist dict lookup（case-insensitive）で弾かれる。`record_type` / `start_date` / `end_date` は `?` parameterized。さらにエラー文へ **user 値を echo しない**設計（`get_record_statistics.py` コメント: 「Do not echo the user-supplied value back ... would otherwise round-trip into the caller LLM's context」）で、prompt-injection の round-trip vector まで潰している。堅牢。
- カタログは #39/#44 で query_records / hash 系の SQLi を確認済みだが、`get_record_statistics` の period 補間経路（whitelist で守る唯一の interpolation 点）は未記載。新規に ✅ 確認。

---

## N6 🚨 envelope 系全 tool の size-budget clamp がネストインデント分を過小評価 → 生 1MB エラー漏れ

### 入力（ライブ）
```
get_workout_route(workout_hash="52795fd2...a6f2", limit=50000)                       # route 7,165 点
get_workout_route(workout_hash="52795fd2...a6f2", with_heart_rate=true, limit=50000) # 同上 + HR
```

### 観測結果（verbatim）
```
Tool result is too large. Maximum size is 1MB.
```
（HR 有無どちらでも同一。= server 自前の `truncated_by_size` envelope ではなく host transport の生エラー）

### 判定: 🚨 defect 候補

`get_workout_route` は `run_query_envelope` 経由で `clip_items_to_size_budget`（950KB budget）を通す設計。にもかかわらず生 1MB エラーが漏れたため、`clip` のロジックを **ソースから verbatim コピーして standalone 再現**（依存の重い import を避けるため純粋関数だけ抜き出し。挙動は同一）:

```
items in / kept   : 7165 / 6154
truncated_by_size : True          ← clip は「安全」と判断
budget            : 950000
ACTUAL wire bytes : 1147015       ← 実 serialize は 1.09MB
host ceiling 1MB  : 1048576
>>> OVER 1MB?     : True (超過 +98439 bytes)
per-item 見積り    : 144 bytes
per-item ネスト実  : 178 bytes  (差 +34 = 過小評価率 23.6%)
```

- 根本原因: `clip_items_to_size_budget` の per-item 見積りは `json.dumps(item, ensure_ascii=False, indent=2)` を **トップレベル**で測る。しかし実 payload では item が `payload → "items" 配列 → item` と **2 段深くネスト**され、各行に追加インデント（約 4 space × 行数）が乗る。結果 1 件 144→178 bytes（+23.6%）の過小評価。budget 950KB を 24% 超過し、clamp 後の kept でも 1.09MB となって host の生エラーが漏れる。
- 設計との乖離: `query.py` の `clip_items_to_size_budget` コメントは「per-item byte 見積りは `run_query_payload` が使う indent=2 / ensure_ascii=False と一致させねばならない ... compact 見積りは約 50% 過小評価し 1MB を破る」と**過小評価問題を認識している**が、fix は indent オプションの一致どまりで **ネスト深さを勘定に入れ忘れ**ており half-right。
- 影響範囲: `run_query_envelope` / `clip_items_to_size_budget` は **envelope 系全 read tool の共有経路**（get_workout_route / query_records / list_workouts / get_heart_rate_samples / list_correlations / list_ecg_readings / list_state_of_mind）。行が十分大きければどれでも生 1MB エラーを出しうる。`#171` の graceful degradation 契約（`truncated_by_size=true` + `next_offset` で残りをページング）が破れ、**caller は next_offset を得られずページング復旧できない**。get_workout_route は密な route + 高 limit で最も踏みやすいだけ。

### 影響 + 推奨修正【推測】
- 影響: severity 中。security escape ではないが #171 契約の破れ。長い route / 大量レコードで agent が「全部取って」と高 limit を指定すると再現。
- 推奨修正【推測】: (1) 堅牢版 = payload 全体を 1 回 serialize して実バイト判定、超過なら item を落として再測（二分探索可）。(2) 応急版 = `DEFAULT_SIZE_BUDGET_BYTES` を過小評価率を吸収する値（例 750KB）へ下げる。(3) or per-item 見積りにネスト分（`インデント深さ × 行数`）を加算。**外れる可能性**: item サイズ分布次第で過小評価率は変動（HR 付き・text_value 長い行ではさらに拡大）。固定マージンより実測ベース (1) が堅い。
- Test Basis: `query.py` `clip_items_to_size_budget` の docstring（過小評価を自認）、`run_query_envelope` の payload 構築。standalone 再現ログ（上記）。

### 既出への類似度
- 未対応欄「JSON wire shape 異常: `get_workout_route(with_heart_rate=true)` で route_points 数万点」の初実測。#21/#22（結果サイズ / 行数 DoS）は `run_custom_query` の別 cap 経路（1MB loud エラーで正しく停止）であり、本件は **envelope tool の size clamp が誤作動して生エラーを漏らす**別事象。新規。

---

## N8 🚨 深いネスト SQL の `RecursionError` が validate_query の catch を素通り → 未処理例外

### 入力（ライブ）
```sql
SELECT (((( ... ((1)) ... ))))   -- 10,000 段ネスト、20,007 字（query max_length=65536 内）
```

### 観測結果（verbatim）
```
Error executing tool run_custom_query: maximum recursion depth exceeded
```
（= tool 自前の graceful `Error: <QueryValidationError>` 文字列ではなく、FastMCP framework の未処理例外ラッパ `Error executing tool <name>: <exc>`）

ローカル再現（pinned `sqlglot==30.11.0`、`validate_query` と同じ `sqlglot.parse(sql, dialect="duckdb")`）:
```
RecursionError in 0.05s     ← hang ではなく即死
```

### 判定: 🚨 defect 候補
- 根本原因: `safety.py` の `validate_query` は `except sqlglot.errors.ParseError` **のみ**を握る。深いネストで sqlglot が投げるのは builtin `RecursionError`（ParseError の系譜外）→ validate_query 素通り → `run_custom_query` の `except QueryValidationError` も素通り → FastMCP 層まで未処理伝播。
- 性質: hang / memory DoS ではない（0.05s で即死。`query` の `max_length=65536` 上限がサイズ律速 DoS を緩和済み）。**「必ず `Error:` 文字列を返す」という tool の typed-error 契約の破れ**。64KB 上限内で自明に作れる病的入力で発火。companion app 等の外部入力経路では安定して未処理例外を誘発可能。

### 影響 + 推奨修正【推測】
- 影響: severity 中。契約破れ / robustness 欠如。RecursionError は通常 recoverable だが、tool 契約としては typed error に正規化すべき。
- 推奨修正【推測】: `validate_query` の parse ガードを `except (sqlglot.errors.ParseError, RecursionError) as exc:` に拡張し QueryValidationError へ。**外れる可能性**: sqlglot が別の builtin 例外（MemoryError 等）を投げる病的入力が他にありうる → parse 前のネスト深度/長さヒューリスティック reject を保険で併用。
- Test Basis: `safety.py` `validate_query`（`except sqlglot.errors.ParseError` のみ）、`run_custom_query.py`（`except QueryValidationError` のみ）。ローカル repro（pinned sqlglot 30.11.0 で RecursionError 0.05s）。

### 既出への類似度
- 未対応欄「巨大 SQL の parser DoS（sqlglot の parse 時間/メモリ）」「大量 UNION/JOIN で AST 肥大」の初実測。ただし観測された failure mode は **DoS（hang）ではなく未処理例外**。サイズ律速 DoS は `max_length=65536` で不発、ネスト深度版が RecursionError 契約破れとして刺さった。新規。

---

## engine hardening ライブ確認（#29 指摘の解消確認）

N1 の観測結果に含まれる通り、v0.6 で以下が実適用されていることをライブ DB で確認:

- `memory_limit` = `1.8 GiB`（`SET '2GB'` に対し DuckDB が内部会計で丸めた実効値）
- `enable_external_access` = `false`
- `lock_configuration` = `true`
- `allow_community_extensions` = `false`

= attack-log #29 の「`memory_limit=50GiB` / `lock_configuration=false` / community extensions 有効が default のまま」という指摘は **v0.6 で解消済み**。これにより #100（recursive CTE memory hang）は fail-fast 化されているはず（本レビューでは memory 軸の再撃は既出のため未実施、hardening 適用の事実のみ確認）。

---

## attack-log 追記候補（本書末尾表へ）

```
| 200 | SELECT current_setting('temp_directory') / 'secret_directory' | scalar introspection で内部 path + username リーク | 🚨 defect 候補: v0.6 #216 は duckdb_settings 等 table-func のみ deny、scalar current_setting が同値を返し素通り。username '<user>' + full path 露出 | v0.6 adversarial |
| 201 | SELECT * FROM duckdb_temporary_files() / duckdb_memory() | 同上の副次 alias | ⚠ validator 通過（denylist 未登録）。temporary_files は spill 中 temp path 露出、memory は memory 統計のみ（path/PII なし）| v0.6 adversarial |
| 202 | SELECT count(*) FROM records a, records b WHERE a.value > b.value（全件 range join）| CPU 律速 self-DoS（no execution timeout）| 🚨 defect 候補: memory_limit=2GB で止まらず timeout も無し、共有 lock 道連れで全 read tool wedge。小規模 probe + code review で機構確定、全件版は未撃（Desktop 再起動リスク回避）| v0.6 adversarial |
| 203 | FROM records SELECT count(*)（DuckDB FROM-first）| parser differential で validator 分類バイパス | ✅ exp.Query 正分類で通過、正常 read | v0.6 adversarial |
| 204 | get_record_statistics(period="week')); DROP TABLE records;--") | period 補間点への SQLi + echo round-trip | ✅ whitelist reject + user 値 no-echo | v0.6 adversarial |
| 205 | get_workout_route(limit=50000) 密な route（7,165 点）| envelope size clamp 過小評価で生 1MB エラー漏れ | 🚨 defect 候補: clip がネストインデント分を 24% 過小評価、truncated_by_size=True と誤判定しつつ実 wire 1.09MB。envelope 系全 tool 共有経路 | v0.6 adversarial |
| 206 | SELECT ((((...((1))...)))) 10,000 段ネスト（20KB, max_length 内）| 深いネストで parser RecursionError | 🚨 defect 候補: validate_query が ParseError のみ catch、RecursionError 素通りで FastMCP 未処理例外化。hang ではなく typed-error 契約破れ（0.05s 即死）| v0.6 adversarial |
| 207 | list_workouts(limit=999999) | limit 上限 clamp の有無 | ✅ _MAX_LIMIT=500 で clamp（description に max 未記載の doc 漏れのみ）| v0.6 adversarial |
```

## 未実施 / 次セッション推奨（本レビューで手を付けていない領域）

- N2 全件版の実測（単独セッション、他結果保全後）
- 巨大 SQL の sqlglot parser DoS（1MB SQL / 大量 UNION での AST 肥大）— hang risk 系、単独実行
- `get_heart_rate_samples` / `get_workout_route` の巨大 nested JSON transport 挙動（数万点）
- タイムゾーン境界（`query_records` の start/end に各種 TZ offset）
- 残る introspection alias（`duckdb_memory` / `duckdb_tables` 等）の path リーク有無の網羅確認

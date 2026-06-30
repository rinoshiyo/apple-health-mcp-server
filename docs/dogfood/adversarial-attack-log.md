# Adversarial attack log (= 実施済み意地悪テスト一覧)

本書は **これまでの adversarial review で既に試した attack vector** の
カタログ。 新規 adversarial review (= Desktop agent や別端末で意地悪
テストを依頼する時) に **「既出を再現するな、 新しい角度を考えろ」** と
渡すための reference。

> ⚡ **使い方**: 下の「Adversarial 依頼 prompt テンプレート」 セクション
> を **本書ごと Desktop agent / 別 session に丸ごと渡す**。 prompt 部分は
> 雛形なので、 依頼者は冒頭 1〜2 文を口語で差し替えても良い (= 「次、
> 意地悪してくれ」 等)。 本書末尾の attack vector 表が既出リスト、 agent
> はそこに無い角度を探す。

## Adversarial 依頼 prompt テンプレート (= ここから下を agent にコピペ)

---

あなたは MCP server `apple-health-mcp-server` の adversarial QA tester
です。 **MCP のテスターとして、 思いつく限り穴をついてください**。

`run_custom_query` / `import_zip` / `list_zips` / `get_import_status` /
各種 read tool に対して、 仕様の隙間・異常系・セキュリティ境界を狙う。
過去の adversarial で発掘した stop-ship (= v0.5.0 #190 SSRF / `sniff_csv`
実データ漏洩) と同等以上の defect を発掘するのが目的。

### 行動規律

- **軽いものから順に試す** (= 副作用の小さい read 系 attack を先に、
  破壊性のある書き込み / DoS 系 attack は後)
- **通常 attack はまとめて実行・まとめて報告で OK** (= 1 メッセージで
  複数試行を畳んで効率良く回す)
- **ただし以下の「致命的リスクのある attack」 は必ず単独で 1 件ずつ
  実行する**:
  - Recursive CTE / `WITH RECURSIVE` / 深い再帰系 (= server hang 実績
    あり)
  - 巨大文字列を tool param に渡す attack (= `source_name` 等に 10K 字
    超を入れる系、 server hang 実績あり)
  - 巨大結果集合を返す attack (= LIMIT 無しで `string_agg` 全件連結等、
    Tool result too large エラーが返るまで時間かかる)
  - 「結果が返るまで何十秒〜何分も待たされる経路」 全般
  - 並列 / Race 系は agent loop で踏めないため対象外
- **致命的リスク attack を単独で回す理由**: hang / 長時間待ちが発生
  すると Claude が止まる、 最悪 **Claude を物理再起動するまで動かなく
  なる**。 そのメッセージ内で前にやっていた成功 attack の結果も**巻き
  戻って失われる** (= バッチで 5 件試行中、 4 件 ✅ で 5 件目で hang
  したら 1-4 件目の結果も消える → 全部やり直し)。 致命的 attack だけ
  単独実行すれば、 犠牲になるのはその attack の 1 件のみで、 前 batch
  は無事 (= v0.5.1 dogfood Phase 3 で recursive CTE と巨大 source_name
  で 2 回 Desktop 再起動を踏んだ実例あり)
- **「やれるやつは全部やれ」**。 思いつく vector を「これは agent ループ
  で踏めない」 と早合点して切るな。 sub-second concurrency 系 (= 同時 2
  連発、 worker 走行中の並列 read) は確かに agent ループで踏めないが、
  それ以外の vector は基本的に試行可能
- **既出の attack vector は再試行しない**。 本書末尾の表に列挙されてる
  vector を見て、 そこに無い角度から攻める。 同じ目的 (= e.g. fs 読み
  取り) でも別の関数 / 別の入力経路を考える

### 報告形式 (= 通常 attack は 1 メッセージで複数畳んで OK、 致命的 attack のみ 1 メッセージ 1 attack)

各 attack について以下を記録:

- **入力**: 投げた tool call と引数を verbatim で
- **観測結果**: envelope / error message そのまま (= truncate しない、
  整形しない、 解釈しない)
- **判定**:
  - ✅ block (= 攻撃成立せず、 typed error or rejection)
  - ⚠ by-design 通過 (= 設計上の carve-out、 既知)
  - 🚨 defect 候補 (= 実害ある or 仕様と挙動の重大な乖離)
  - ⏳ 未確定 (= さらに検証必要)
- **defect 候補なら**: 影響 + 推奨修正方針【推測】 を 1-3 文で
- **既出への類似度**: 本書末尾の表の attack # と比較、 「#11 (parquet_scan)
  と同経路だが別関数」 等の差分明示

### Test Basis 引用必須

- 「あの DESCRIPTION でこう書いてあった」 「issue #N の acceptance
  criteria」 「CHANGELOG の v0.5.1 ### Security 行」 等、 一次情報の
  突合先を結果に添える
- **推測ベースの defect 報告はしない** (= 確証取れた挙動だけ報告)
- 不確かなら 「【推測】 〜の可能性、 検証要」 と明示

### 重大な禁止事項

- **server を hang させたら正直に報告する** (= 隠さない)。 user に
  「Claude Desktop の物理再起動が必要かもしれません」 と促す
- **「既に試された」 「scope 外」 と勝手に切らない**。 既出と判断する場合は
  本書の attack # を引用して理由を明示
- **defect の重要度を勝手に下げない**。 「self-DoS だから低い」 等の
  判定は user の領分

### 終了条件

- 「これ以上思いつく vectors が無い」 と判断したら、 試行した attack
  一覧 (= 番号 + 1 行サマリ) と defect 候補リスト + UX 改善候補 +
  新規発見 attack vector の **本書追記候補** を出して終了
- user から「終わり」 と言われたら終了
- 5 連続で ✅ block が続いたら一旦区切って「次に攻める角度を考えてる」
  と user に報告し、 指示待ちでも可

### 既出 attack vector のリスト

(= 下記「凡例」 section + 各カテゴリの attack vector 表を参照、 既出は
基本的に再試行しない)

---

## 本書の運用規律 (= 書き手 / 読者向け)

**書き手規約:**
- 1 行 = 1 attack vector (= 入力 + 経路 + 結果概要 + 起点 review)
- 結果は v0.5.1 (= 最新 release) **時点** で「block 済 (= ✅)」 /
  「by-design 通過 (= ⚠ 既知の carve-out)」 / 「修正済 (= 🔧 既に fix
  された経路)」 / 「未対応 (= ⏳ issue 起票済)」 のいずれか
- attack vector を新規追加する時は表末尾に追記、 既存行は触らない
  (= 履歴として保つ)
- 「結果」 列の値が release で変わったら、 新行追加 (= 同じ attack で
  異なる version の結果を別行に) — 上書きしない

**読者規約 (= 新規 adversarial 担当の agent):**
- 本表に **同じ attack vector が既に列挙されてる場合、 それは試さない**
- 攻めるなら「同じ目的 (= e.g. fs 読み取り) で別の関数 / 別の入力経路」
  を考える
- 列挙されてない attack vector を考えるのが本来の意地悪 review の価値

## 凡例

| 記号 | 意味 |
|---|---|
| ✅ | block 済 (= 攻撃成立せず、 typed error or rejection) |
| ⚠ | by-design 通過 (= 設計上の carve-out、 既知) |
| 🔧 | 修正済 (= かつて成立した攻撃、 後 release で fix) |
| ⏳ | 未対応 (= issue 起票済、 future fix 予定) |

## SQL safety / `run_custom_query` 系 attack vectors

| # | attack 入力 | 狙い | 結果 (v0.5.1) | 起点 |
|---|---|---|---|---|
| 1 | `SELECT ...; DROP TABLE records;` | 複文で DDL 注入 | ✅ `Only a single SQL statement is allowed (got 2)` | v0.5.0 adv 1 |
| 2 | `COPY (SELECT ...) TO '<path>/leak.csv'` | ファイル書き込み | ✅ `Only SELECT / WITH queries are allowed` | v0.5.0 adv 1 |
| 3 | `SELECT * FROM glob('<path>/*')` | ディレクトリ列挙 | ✅ `Function 'glob' is not allowed` | v0.5.0 adv 1 |
| 4 | `SELECT read_csv('/etc/passwd')` | fs 読み取り (= csv) | ✅ `Function 'read_csv' is not allowed` | v0.5.0 adv 1 |
| 5 | `SELECT read_csv_auto('...')` | fs 読み取り (= csv auto) | ✅ denylist reject | v0.5.0 adv 1 |
| 6 | `SELECT read_text('...')` | fs 読み取り (= text) | ✅ denylist reject | v0.5.0 adv 1 |
| 7 | `SELECT read_blob('...')` | fs 読み取り (= blob) | ✅ denylist reject | v0.5.0 adv 1 |
| 8 | `SELECT read_parquet('...')` | fs 読み取り (= parquet) | ✅ denylist reject | v0.5.0 adv 1 |
| 9 | `SELECT read_json('...')` / `read_json_auto` / `read_ndjson` | fs 読み取り (= json 系) | ✅ denylist reject | v0.5.0 adv 1 |
| 10 | `WITH x AS (SELECT * FROM read_text(...)) SELECT * FROM x` | CTE 内に隠した fs 読み取り | ✅ 再帰チェックで reject | v0.5.0 adv 1 |
| 11 | `SELECT * FROM parquet_scan('<path>/x.parquet')` | denylist alias 漏れで fs 読み取り | 🔧 v0.5.0 では IO Error で fs 到達、 v0.5.1 #190 で engine-level lockdown + denylist 追加で `Permission Error` block | v0.5.0 adv 1 → v0.5.1 |
| 12 | `SELECT * FROM parquet_metadata('<path>/win.ini')` | denylist alias 漏れで fs 読み取り | 🔧 v0.5.0 では win.ini 実読、 v0.5.1 で block | v0.5.0 adv 1 → v0.5.1 |
| 13 | `SELECT * FROM parquet_schema('<path>/win.ini')` | 同上 (= schema 系 alias) | 🔧 v0.5.0 では fs 到達、 v0.5.1 で block | v0.5.0 adv 2 → v0.5.1 |
| 14 | `SELECT * FROM sniff_csv('<path>/win.ini')` | denylist alias 漏れで **fs 内容そのまま返却** | 🔧 v0.5.0 では win.ini 1 行目をカラム名として返却、 v0.5.1 で block | v0.5.0 adv 2 → v0.5.1 |
| 15 | `SELECT * FROM parquet_scan('https://...')` | httpfs egress / SSRF | 🔧 v0.5.0 では外部 URL fetch 成立、 v0.5.1 で `enable_external_access=false` で block | v0.5.0 adv 2 → v0.5.1 |
| 16 | `SELECT * FROM read_duckdb('<path>/other.duckdb')` | DuckDB ファイル直読 | ✅ engine-level lockdown で `Permission Error` block (= ただし denylist 未追加 = defense-in-depth 不完全、 v0.6 検討) | v0.5.1 dogfood 後 確認 |
| 17 | `SELECT ... FROM duckdb_settings()` | introspection で内部 path 取得 | ⚠ by-design 通過 (= 外部リソース取得ではないため engine lockdown 対象外、 `temp_directory` 等が出る、 低リスク) → ⏳ v0.6 #216 で denylist 追加検討 | v0.5.0 adv 1 |
| 18 | `ATTACH '<path>/attacker.db' AS f` | file-backed ATTACH | ✅ `Permission Error: file system operations are disabled` | v0.5.1 integration test pin |
| 19 | `ATTACH 'https://attacker.example/x.db' AS f` | URL-backed ATTACH | ✅ engine-level lockdown で block | v0.5.1 integration test pin |
| 20 | `INSTALL httpfs` / `LOAD httpfs` | 拡張ロード | ✅ engine-level lockdown で `Permission Error` | v0.5.1 integration test pin |
| 21 | 1 行に巨大 payload (= `string_agg` で全 hash 連結) | 結果サイズ DoS | ✅ `Tool result is too large. Maximum size is 1MB.` の loud エラー | v0.5.0 adv 1 |
| 22 | LIMIT 無し巨大 SELECT (= HeartRate 全件) | 結果行数 DoS | ✅ `row_count: 1000` + `truncated: true` で正直通知 | v0.5.0 adv 1 |
| 23 | `read_text_auto` / `read_json_auto` / `read_blob` | denylist 漏れ alias 試行 | ✅ deny list で reject | v0.5.1 dogfood Phase 3 |
| 24 | `read_duckdb('<path>/health.duckdb', schema_name=>'main', table_name=>'imports')` | DuckDB 直読 alias | ⚠ engine-level lockdown で `Permission Error` block、 ただし **denylist 未登録 = defense-in-depth 不完全**、 さらに実存ファイル指定で UTF-8 デコードエラーで envelope 壊れる | ⏳ v0.6 検討 (= v0.5.1 dogfood Phase 3 defect #1) |
| 25 | `read_ndjson_objects` / `read_json_objects` 系 | 同上 alias 群 | ⚠ engine-level lockdown で block、 denylist 未登録 = #24 と同根 | ⏳ v0.6 検討 (= #24 と同 fix で対応) |
| 26 | `SET enable_external_access=true` 等の PRAGMA / SET bypass | 設定書き換え試行 | ✅ multi-statement と PRAGMA は X1 (= 複文 / DDL reject) で塞ぎ済み、 SELECT 内 `current_setting` で false 固定確認 | v0.5.1 dogfood Phase 3 |
| 27 | `'https://...'` / `'s3://...'` を quoted-path table reference として | URL 経由 escape | ✅ `Quoted-path table reference reject` | v0.5.1 dogfood Phase 3 |
| 28 | `SELECT ... FROM duckdb_databases() / duckdb_tables() / duckdb_indexes()` | introspection 拡張 | ⚠ by-design 通過 (= #17 `duckdb_settings` と同根、 `temp_directory` 等 path 取得可、 ⏳ v0.6 #216 で denylist 追加検討に含める) | v0.5.1 dogfood Phase 3 |
| 29 | `SELECT name, value FROM duckdb_settings()` で hardening 不足設定の発見 | DuckDB 設定 audit | 🚨 **defect 候補**: `memory_limit=50.0 GiB` / `lock_configuration=false` / `allow_community_extensions=true` / `autoload_known_extensions=true` 等 default のまま = defect #2 #4 の根本原因 | ⏳ v0.6 必須 (= v0.5.1 dogfood Phase 3 defect #3) |
| 100 | `WITH RECURSIVE bomb(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM bomb WHERE n < 100000000) SELECT count(*) FROM bomb` | recursive CTE で memory 食い尽くし self-DoS | 🚨 **defect 候補**: **MCP server プロセス完全 hang**、 4 分以上応答なし、 Claude Desktop **物理再起動でのみ復旧**。 `memory_limit=50GB` default のため materialize で食い尽くす | ⏳ v0.6 必須 (= v0.5.1 dogfood Phase 3 defect #2、 #29 hardening で物理解消可) |
| 101 | `GENERATE_SERIES(1, 1000000)` を SELECT | streaming generate cap 確認 | ✅ `max_rows=1000` で streaming cap、 materialize しない | v0.5.1 dogfood Phase 3 |
| 102 | `SELECT REPEAT('A', 1000000)` で巨大文字列 build | 結果文字列サイズ DoS | ✅ length() で受け止め、 wire 経由は 1MB エラーで停止 | v0.5.1 dogfood Phase 3 |
| 103 | **`query_records(record_type=..., source_name='A' × 約 12,000 字)`** | tool param に巨大文字列で prepared statement bind memory DoS | 🚨 **defect 候補**: **MCP server プロセス hang、 Claude Desktop 物理再起動でのみ復旧**。 companion app で外部入力経路ができた時 critical | ⏳ v0.6 必須 (= v0.5.1 dogfood Phase 3 defect #4、 #29 hardening + tool param max length validation の両方推奨) |

## 入力境界 / 入力 validation 系

| # | attack 入力 | 狙い | 結果 (v0.5.1) | 起点 |
|---|---|---|---|---|
| 30 | `import_zip(id="abc")` (= 4 文字未満) | id len 境界 | ✅ `invalid_id` envelope | v0.5.0 adv 1 |
| 31 | `import_zip(id="<65 char hex>")` (= 64 超) | id len 境界 (上限) | ✅ `invalid_id` envelope | v0.5.0 adv 1 |
| 32 | `import_zip(id="<SHA8 大文字>")` | case-sensitivity | ⚠ 受理 (= trim + lowercase 寛容、 v0.5.1 #191 で doc を実態に合わせて整合) | v0.5.0 adv 1 |
| 33 | `import_zip(id=" <sha8> ")` (= 前後空白) | trim 挙動 | ⚠ 受理 (= 同上) | v0.5.0 adv 1 |
| 34 | `import_zip(id="zzzz")` (= 非 hex) | charset 検証 | ✅ `invalid_id` envelope | v0.5.0 dogfood F4 |
| 35 | `query_records(limit=0)` | limit 下限境界 | ✅ `limit must be >= 1` | v0.5.0 adv 1 |
| 36 | `query_records(limit=999999)` | limit 上限境界 | ✅ 1000 に clamp、 `next_offset: 1000` | v0.5.0 adv 1 |
| 37 | `query_records(offset=-5)` | offset 負値 | ⚠ 先頭ページ返却 (= 無言 coerce、 害なし) | v0.5.0 adv 1 |
| 38 | `query_records(offset=999999999)` | offset 上限超過 | ✅ `items: []` / `next_offset: null` | v0.5.0 adv 1 |
| 39 | `query_records(start_date="2020-01-01' OR '1'='1")` | SQL injection 試行 | ✅ `Conversion Error: invalid timestamp field format` (= timestamp cast、 文字列連結なし) | v0.5.0 adv 1 |
| 40 | `query_records(start_date > end_date)` | 日付逆転 | ✅ `[]` 空配列 (= error にせず空) | v0.5.0 adv 1 |
| 41 | `import_zip(id="ｆｆｃ７２ａ０ｆ")` (= 全角 hex) | Unicode 攻撃 / 全角 charset | ✅ `invalid_id` | v0.5.1 dogfood X2.5 |
| 42 | `import_zip(id="<sha8>malicious")` (= mid-string 非 hex 混入) | charset 中途破壊 | ✅ `invalid_id`、 server クラッシュなし | v0.5.1 dogfood X2.4 |
| 43 | NUL byte / 絵文字 / RTL override / 空文字を tool param に注入 | Unicode / 制御文字 attack | ✅ typed empty 返却 (= MCP transport が NUL byte sanitize) | v0.5.1 dogfood Phase 3 |
| 44 | `get_workout_details` / `get_correlation_details` / `get_ecg_data` / `get_heart_rate_samples` / `get_workout_route` の hash 引数に SQL 片注入 | hash 系 SQL injection (= 5 経路) | ✅ 全 5 経路 parameterized query で escape、 X1 と整合 | v0.5.1 dogfood Phase 3 |
| 45 | `query_records(offset=9223372036854775807)` (= INT64 max) | 整数オーバーフロー | ⚠ DuckDB の生エラー `Conversion Error: Type INT128 with value ... can't be cast` 露出 (= UX 改善候補 #5) | ⏳ v0.6 (= v0.5.1 dogfood Phase 3 UX #5) |
| 46 | `query_records(limit=-1)` / `every_nth=-1` 等の負値 | 整数下限境界 | ✅ typed validation reject | v0.5.1 dogfood Phase 3 |
| 47 | `import_zip(id="deadbeef" × 約 2,000 字)` | 巨大 id payload | ⚠ `invalid_id` で reject されるが message に id 全体 echo back (= context 無駄消費、 UX 改善候補 #4) | ⏳ v0.6 (= v0.5.1 dogfood Phase 3 UX #4) |
| 48 | SELECT 構文系 (= multi-statement / コメント注入 / `pragma_database_list`) | SQL 解析 bypass | ✅ X1 と整合、 コメント内 SQL 片は無効化 | v0.5.1 dogfood Phase 3 |

## Path / env attacks

| # | attack 入力 | 狙い | 結果 (v0.5.1) | 起点 |
|---|---|---|---|---|
| 60 | `APPLE_HEALTH_EXPORT_ZIPS_DIR` 未設定で `import_zip` / `list_zips` | env 未設定境界 | ✅ `export_zips_dir_not_set` envelope | v0.5.1 dogfood F5 |
| 61 | `APPLE_HEALTH_EXPORT_ZIPS_DIR=..\..\..\Windows\System32` (= relative directory escape) | path traversal 試行 | ✅ `list_zips: zips:[]` + relative のまま `export_zips_dir` フィールドに表示、 `import_zip: id_not_found` (= 中身が apple health zip じゃない) | ⚠ UX 改善候補: 絶対 path 正規化 (= UX #1) | v0.5.1 dogfood X3.1 / X3.2 |
| 62 | `APPLE_HEALTH_DB=/etc/passwd` / `con` (Windows 予約名) / 空文字 | DB path 異常値 | ⏳ MCPB user_config UI に該当項目が露出してないため、 MCPB 経由では永久に test 不可。 OS env 経由でしか踏めない | スコープ外 |

## ZIP attacks (= §X4 系)

`tests/fixtures/adversarial/` 配下に pre-generated。

| # | attack ZIP | 狙い | 結果 (v0.5.1) | 起点 |
|---|---|---|---|---|
| 80 | 0 byte zip | empty ZIP boundary | ✅ `list_zips: zip_status:invalid_zip` (= id:e3b0c442 空 sha 定数)、 `import_zip: reason:invalid_zip + Re-download 誘導` | v0.5.1 dogfood X4.1 |
| 81 | 1 byte zip (= `P` のみ) | partial ZIP boundary | ✅ 同上 (= id:5c62e091) | v0.5.1 dogfood X4.2 |
| 82 | multi-entry ZIP + `apple_health_export/export.xml` が 0 byte | 0 byte XML 完走 vs error 終端 | ✅ **実装の方が厳格**: `zip_status:valid_apple_health` → `status:queued → status:error, reason:run_import_failed, "unrecoverable XML syntax error: no element found (line 0)"`。 test plan 期待 (= record_count=0 完走) より良い設計 | v0.5.1 dogfood X4.3 |
| 83 | zip slip attempt (= `../../../tmp/zip-slip-escape.txt` path entry) | zip slip 攻撃 | ✅ `status:queued → status:ok, records_added:0, duration_secs:1.74` (= 完走するが records 追加なし、 host fs 脱出書き込みなし) | v0.5.1 dogfood X4.4 |
| 84 | byte-identical ZIP を別ファイル名で 2 個配置 | 同 sha 別名の cache hit | ✅ 2 entries 両方 listed, **同 id `b91758ae`** (= sha8 共有)、 1 個 import 後の 2 個目は idempotent ok (= records_added:0, already_imported_at) | v0.5.1 dogfood X4.5 |
| 85 | mtime=2100-01-01 (= 未来) の ZIP | future-date assertion crash | ✅ `mtime:"2100-01-01T00:00:00+00:00"` ISO 表示、 sha 不変、 cache key (size, mtime) 影響なし | v0.5.1 dogfood X4.6 |
| 86 | XML valid だが Record の start_date 属性が空の ZIP | XML 通すが DuckDB ingest で失敗 | ⚠ typed envelope: `status:queued → status:error, reason:run_import_failed, "Conversion Error: invalid timestamp field format: \"\", ... when casting from source column start_date"` (= DuckDB 生エラー露出、 UX 改善候補 #2) | ⏳ v0.6 (= v0.5.1 dogfood UX #2 / X4.7) |

## Job / state 系

| # | attack 入力 | 狙い | 結果 (v0.5.1) | 起点 |
|---|---|---|---|---|
| 50 | done 済 job_id を再 `get_import_status` | 永続性 / 冪等 read | ✅ 永続 ok envelope を再返却 | v0.5.0 adv 1 |
| 51 | 正しい形式の偽 job_id (= `ij_<future-ts>_<sha8>_<rand>`) | 未存在 id | ✅ `job_not_found` envelope | v0.5.0 adv 1 |
| 52 | 不正形式 job_id (= `ij_nope` / 空文字) | 形式違反 | ✅ `job_not_found` envelope | v0.5.0 dogfood F6 |
| 53 | 同 sha 同時 2 連発 (= multi-launch guard) | TOCTOU race | ⏳ 未踏 (= agent ループでは sub 秒同時を再現不可、 unit test で pin 済) | v0.5.0 dogfood B2 |
| 54 | import 中の他 read tool 並行 | writer lock 競合 | ⏳ 未踏 (= worker 走行中の sub 秒窓に差し込めず、 unit test で pin 済) | v0.5.0 dogfood D |
| 55 | cross-session 永続性 (= server 物理再起動 → 新 session で過去 job_id を `get_import_status`) | Tier 越境永続 | ✅ `import_jobs` テーブルが DuckDB persist、 新 server プロセス起動後も全 job 状態が読める、 wire shape 完全保持 (= 24 時間前の job も含む)、 偽 job_id は `job_not_found` design contract 維持 | v0.5.1 dogfood X5.4 |

## Server crash / 破壊耐性

| # | attack | 狙い | 結果 (v0.5.1) | 起点 |
|---|---|---|---|---|
| 70 | DROP / DELETE / COPY / 複文の連続試行 後の整合確認 | 部分的破壊が残るか | ✅ records / import_jobs / table 数いずれも無傷 (= 21 tables 維持) | v0.5.0 adv 1+2 §7 |
| 71 | import 中に Claude Desktop 強制終了 → 再起動 | orphan job recovery | ✅ boot sweep が `server_restarted_while_running` envelope で終端 | v0.5.0 dogfood B3 |
| 72 | 既存 v=5 DB に v0.5.0 server を被せて `import_zip` | schema 差分での crash | 🔧 v0.5.0 では raw `Catalog Error: Table import_jobs does not exist`、 v0.5.1 #188 で `schema_outdated` envelope に typed 化 | v0.5.0 dogfood + v0.5.1 |

## 未対応 / 任意で攻めるべき領域 (= まだ試されてない例、 v0.5.1 dogfood 後の最新版)

以下は **本表の attack vectors に含まれてない**、 = 新規 adversarial で
試す価値ある領域。 attack vector を新規に思いついたら本表に追記。

v0.5.1 dogfood Phase 3 で **大量に embraced されたため、 旧版にあった
多くの項目は本表本体に移動済**。 残るは以下:

- **巨大 SQL の parser DoS**: 1MB の SQL 文字列を `run_custom_query` に
  投げて parser を疲弊させる (= sqlglot の parse 時間 / メモリで stall
  する可能性、 hang risk 系なので致命的 attack 規律で単独実行)
- **巨大 SQL に大量 UNION / JOIN**: `SELECT 1 UNION SELECT 2 UNION ... × 10000`
  等の長 query で AST 巨大化 (= parser DoS 別 vector)
- **DuckDB execution timeout の不在**: 30 秒超の query を投げ続けて
  server が止まることを agent ループ内で観測可能か (= 致命的 attack)
- **Concurrent attack (= 環境外スクリプトで sub 秒並列)**: #53, #54 を
  agent ループ外から踏む (= sub-process script 前提)
- **MCP transport boundary**: stdio の SIGPIPE / 切断中の tool 呼出し
  /  巨大 envelope (= 1MB 直前) の transport 挙動
- **`APPLE_HEALTH_DB` の異常値**: `/etc/passwd` / Windows 予約名 / 空文字
  (= #62 で記述、 MCPB user_config 経路では永久に不可、 OS env 経由のみ)
- **import_zip の同 ZIP 連続再投入**: 100 連発で sha cache が劣化するか
  (= #21 と異なる、 同 sha repeated で cache hit 率を観測)
- **`get_import_history` 行数爆破**: 数千 import を順次走らせて history
  が膨大になった時の wire shape / pagination 挙動
- **タイムゾーン boundary**: `query_records(start_date="2025-12-31T23:59:59")`
  + 各種 TZ で日付境界処理 (= 既存 #39/#40 は SQLi 試行と単純逆転のみ)
- **正規表現 DoS**: tool param に正規表現 catastrophic backtracking 誘発
  pattern を入れる (= 該当 tool が regex を使ってる前提、 確認要)
- **JSON wire shape 異常**: tool 結果が巨大 nested JSON になるケース
  (= e.g. `get_workout_route(with_heart_rate=true)` で route_points
  数万点) の transport 挙動

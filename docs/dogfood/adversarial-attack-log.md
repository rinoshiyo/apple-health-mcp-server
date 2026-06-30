# Adversarial attack log (= 実施済み意地悪テスト一覧)

本書は **これまでの adversarial review で既に試した attack vector** の
カタログ。 新規 adversarial review (= Desktop agent や別端末で意地悪
テストを依頼する時) に **「既出を再現するな、 新しい角度を考えろ」** と
渡すための referense。

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

## Job / state 系

| # | attack 入力 | 狙い | 結果 (v0.5.1) | 起点 |
|---|---|---|---|---|
| 50 | done 済 job_id を再 `get_import_status` | 永続性 / 冪等 read | ✅ 永続 ok envelope を再返却 | v0.5.0 adv 1 |
| 51 | 正しい形式の偽 job_id (= `ij_<future-ts>_<sha8>_<rand>`) | 未存在 id | ✅ `job_not_found` envelope | v0.5.0 adv 1 |
| 52 | 不正形式 job_id (= `ij_nope` / 空文字) | 形式違反 | ✅ `job_not_found` envelope | v0.5.0 dogfood F6 |
| 53 | 同 sha 同時 2 連発 (= multi-launch guard) | TOCTOU race | ⏳ 未踏 (= agent ループでは sub 秒同時を再現不可、 unit test で pin 済) | v0.5.0 dogfood B2 |
| 54 | import 中の他 read tool 並行 | writer lock 競合 | ⏳ 未踏 (= worker 走行中の sub 秒窓に差し込めず、 unit test で pin 済) | v0.5.0 dogfood D |

## Server crash / 破壊耐性

| # | attack | 狙い | 結果 (v0.5.1) | 起点 |
|---|---|---|---|---|
| 70 | DROP / DELETE / COPY / 複文の連続試行 後の整合確認 | 部分的破壊が残るか | ✅ records / import_jobs / table 数いずれも無傷 (= 21 tables 維持) | v0.5.0 adv 1+2 §7 |
| 71 | import 中に Claude Desktop 強制終了 → 再起動 | orphan job recovery | ✅ boot sweep が `server_restarted_while_running` envelope で終端 | v0.5.0 dogfood B3 |
| 72 | 既存 v=5 DB に v0.5.0 server を被せて `import_zip` | schema 差分での crash | 🔧 v0.5.0 では raw `Catalog Error: Table import_jobs does not exist`、 v0.5.1 #188 で `schema_outdated` envelope に typed 化 | v0.5.0 dogfood + v0.5.1 |

## 未対応 / 任意で攻めるべき領域 (= まだ試されてない例)

以下は **本表の attack vectors に含まれてない**、 = 新規 adversarial で
試す価値ある領域。 attack vector を新規に思いついたら本表に追記。

- **ZIP の意地悪 (= §X4 系)**: zip slip / 巨大 export.xml / 0 byte
  export.xml / 同 sha 別名 / 未来 mtime / 壊れ XML
  (= `docs/dogfood/v0-5-1-test-plan.md` §X4 + `tests/fixtures/adversarial/`
  の pre-generated fixtures で fixtures は揃ってるが、 実機 attack 結果は
  まだ本 log に未追加)
- **Path / env 系**: `APPLE_HEALTH_EXPORT_ZIPS_DIR` に `../../../etc` /
  Windows 予約名 / 巨大絶対 path / 空文字
- **MCPB bundle 経路特有**: agent prompt 経由で adversarial query を投げ
  て denylist が effective か (= in-process unit test では simulate
  不可)
- **`run_custom_query` の introspection 系拡張**: `duckdb_extensions()` /
  `duckdb_databases()` 等 path / 環境情報を返す系 (= #17 と同系統だが
  別関数群)
- **Unicode / 制御文字 in id**: NULL byte 注入 / 全角 hex / RTL override
  等
- **巨大 SQL**: 1MB の SQL 文字列 (= parser DoS)
- **WITH RECURSIVE 深い再帰**: stack overflow 狙い
- **Concurrent attack (= 環境外スクリプトで sub 秒並列)**: #53, #54 を
  agent ループ外から踏む

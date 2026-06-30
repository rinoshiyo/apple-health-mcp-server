# v0.5.1 dogfood test plan

v0.5.1 の主役は 2 本:

1. **`run_custom_query` の engine-level lockdown** (#190 stop-ship,
   `SET enable_external_access = false`) — v0.5.0 adversarial review
   で発覚した SSRF / 任意 fs 読み取り経路を engine 設定 1 つで封鎖。
2. **`schema_outdated` typed envelope** (#188) — v0.4 系 DB を
   v0.5.1 server で開いた時に raw `Catalog Error: import_jobs does
   not exist` を露出させず、 typed envelope で recovery 経路に誘導。

副題は wire-shape 系: `list_zips` hint を async に (#187)、
`get_import_history` の `processing_secs` alias (#189)、 `import_zip`
の `id` 寛容仕様 doc 化 (#191)。

v0.5.0 plan (`docs/dogfood/v0-5-0-test-plan.md`) の流儀を踏襲。
agent ループでは踏みづらい並行系 (D / B2) は v0.5.0 でも未踏のままに
した経緯あり、 同じスコープ判断で v0.5.1 でも実施対象外。

## 特に踏みたいポイント (= 実機 only で初めて catch できる経路)

unit / integration テストは 607 件全 green、 100% branch coverage 維持
済なので「一般動作の確認」 ではなく **テストが構造的に踏めない経路** を
優先する。

- **A1.4 (= `get_import_history` が v=5 DB で raw column-missing error
  を出さない)**. v0.5.1 PR #200 の code-review で発覚した gate 漏れ
  経路。 `dedup_skipped` column が v=5 imports shape に存在しない →
  v0.5.0 では raw `Catalog Error: Referenced column "dedup_skipped"
  not found` が露出していた。 v0.5.1 で gate 追加済だが、 0.4.legacy
  DB を実機で開く以外に「raw error が出ないこと」 の確認手段が無い。
- **B2.1 (= `current_setting('enable_external_access')` が `"false"` を
  実機で返す)**. `SET enable_external_access = false` が本当に効いて
  るかは、 in-process unit test では engine 起動時の同 setting で確認
  しているが、 **MCPB bundle 起動経路で同設定が落ちないか** は実機
  でしか分からない。 これが落ちると denylist の網の目を裏で外せる。
- **C2.2 (= 同 import の `processing_secs` vs `duration_secs` で
  数秒差を実測)**. unit test では mock 値しか pin できない (= 実 ZIP
  展開時間に依存)。 集客 narrative の F&Q 素材
  (「何故 2 つの数字がある?」) に直接使えるので、 1 件だけでも実測
  値を H3 にメモする価値あり。
- **E3 (= MCPB bundle 経由で `run_custom_query` の denylist が
  agent 経由でも reject する)**. agent prompt 経由で adversarial
  query を投げる経路は unit test では simulate できない。 in-process
  ではない MCPB 経路で denylist 判定が effective かを確認。

逆に **agent ループで踏めない経路** (= スコープ外):

- B2 multi-launch guard (sub-second 同時 2 連発)
- D import 中の他 tool 並行 (worker 走行中の sub-second 窓)

これらは v0.5.0 dogfood でも未踏のまま、 unit test (concurrent_*
系) でカバー済。 ここで踏もうとして時間溶かさない。

任意で踏む:

- **A5 (= v=6 stamp + `import_jobs` DROP の corruption corner)**.
  Claude Desktop を終了 → DuckDB CLI で `health.duckdb` を開いて
  `DROP TABLE import_jobs` → 再起動 → `list_zips` が schema_outdated
  envelope を返すか確認。 unit test
  (`test_check_data_state_flags_populated_db_with_missing_import_jobs`)
  で pin 済なので実機で踏む必要性は低い。 skip 可。

## 0. セットアップ

zion 物理操作前提。 順に消化。

- [ ] **0.1** Claude Code (CLI) で v0.5.1 を pin インストール:
      `uvx --from 'apple-health-mcp-server==0.5.1' apple-health-mcp-server --version`
      → `0.5.1` が表示される。 `@latest` だけだと `uvx` キャッシュ
      で古い版が残る罠あり ([[reference-pypi-uvx-pre-release-at-latest-pitfall]])
      のため `==0.5.1` 明示。
- [ ] **0.2** Claude Desktop (MCPB bundle):
      <https://github.com/rinoshiyo/apple-health-mcp-server/releases/tag/v0.5.1>
      から `.mcpb` を DL → ダブルクリックでインストール → Settings →
      Extensions に「Apple Health (0.5.1)」 表示。
- [ ] **0.3** `APPLE_HEALTH_EXPORT_ZIPS_DIR` を実 export 置き場
      (例: `D:\data\OneDrive - itrans\apple-health-exports`) に設定。
- [ ] **0.4** **2 系統 DB を準備** (G 章で使う):
      - **fresh DB**: `APPLE_HEALTH_DB` を空 path に設定し、 v0.5.1
        サーバが新規作成する DB で A〜F を走らせる。
      - **legacy v=5 DB の退避コピー**: 前回 dogfood で残した v0.4
        系 DB ファイル (= `schema_version=5`、 `import_jobs` 不在)
        を `health-v0.4.duckdb` 等にコピーして保存。 G 章で
        `APPLE_HEALTH_DB` をこの path に向けて schema_outdated 経路を
        踏む。

## A. schema_outdated typed envelope (= v0.5.1 主役 1/2)

v0.4 系 DB を v0.5.1 server で開いた時の挙動。 v0.5.0 dogfood で
「raw `Catalog Error: import_jobs does not exist!` が露出した」 ボトル
ネックを envelope 経由に切り替える。 read tool 経路は v0.4.1
(#156) で既に NEEDS_REIMPORT 化済、 v0.5.1 で **write tool 4 経路**
(`list_zips` / `import_zip` / `get_import_status` /
`get_import_history`) にも gate 追加。

### A1. v=5 DB に対する 4 write tool の gate

`APPLE_HEALTH_DB` を 0.4.legacy DB に向けて server 起動。

- [ ] **A1.1** `list_zips` → `{"state":"NEEDS_REIMPORT",
      "reason":"schema_outdated","suggested_action":"call_import_zip",
      "human_message":"..."}` が返る。 raw Catalog Error が**出ない**
      ことを確認。
- [ ] **A1.2** `import_zip(id="aaaaaaaa")` (id 値は何でもよい、 gate
      が validation より先に発火する設計) → 同 envelope。 `INSERT
      INTO import_jobs` には到達しない (= worker spawn されない)。
- [ ] **A1.3** `get_import_status(job_id="ij_anything")` → 同 envelope。
      `job_registry.get_job` に到達しない。
- [ ] **A1.4** `get_import_history` → 同 envelope (= `dedup_skipped`
      column を SELECT する手前で gate)。 v=5 imports shape の
      column-missing error が**出ない**ことを確認。

### A2. envelope 内容の wire-shape 確認

`payload["reason"] == "schema_outdated"` (= 安定した enum 識別子) で
agent が分岐可能か。 v0.5.0 時点では `"database was imported under
an older package release; ..."` の prose だったが v0.5.1 で固定 enum
に変更。

- [ ] **A2.1** `state`, `reason`, `suggested_action`, `human_message`
      の 4 キーが揃ってる。
- [ ] **A2.2** `human_message` 内に「`import_zip(id=...)` を呼べ」
      系の誘導文がある (= `list_zips` 呼出しを再度推奨しない =
      list_zips loop 回避、 PR #200 code-review #4)。

### A3. fresh-reset → 再 ingest

`import_zip(id=<実 sha>)` をピコ判断で呼ぶと、 importer の
fresh-reset 経路が起動 (= `schema_version_is_stale` 検知 →
`reset_db_for_fresh_import` → `ensure_schema` → 再 ingest)。
v=5 → v=6 in-place migration **ではない** (= 既存データは消える)。
ターミナル不要なのが v0.4.1 思想。

- [ ] **A3.1** legacy v=5 DB を path 指定して `import_zip(id=<sha>)`
      を実行 → 初回は schema_outdated envelope (上述) → ピコ判断で
      ID 指定再呼出し時、 worker spawn される (= fresh-reset
      経由で `import_jobs` 作成済)。 (※ v0.5.1 の挙動を厳密に再確認:
      fresh-reset は server 起動時か、 import_zip 呼出し時か。
      issue #188 本文 + #156 の挙動次第)。
- [ ] **A3.2** 完走後 `get_server_info` → `record_count > 0`、
      `version="0.5.1"`。 v=5 DB の元データは消えてる (= 期待通り)。

### A4. healthy DB は無影響

fresh DB / 既に v=6 stamped + import_jobs 存在 DB では 4 write tool
が普通に動く。

- [ ] **A4.1** A1 を fresh DB に向けて再実行 → 全 tool が schema_outdated
      ではなく通常の response を返す。

### A5. corruption corner case (= v=6 stamp + import_jobs DROP)

v0.5.1 の新 branch (= `has_rows AND jobs_missing`) を踏む。

- [ ] **A5.1** fresh DB を 1 回 import 完了させてから、
      `run_custom_query("DROP TABLE import_jobs")` ... と思ったが
      `run_custom_query` は DDL を validator で reject。 → 代わりに
      Claude Desktop 終了して別ツール (e.g. DuckDB CLI) で
      `health.duckdb` を開き、 `DROP TABLE import_jobs` を実行。 再
      起動して `list_zips` → schema_outdated envelope (= 新 branch
      が catch)。 ピコの判断で skip 可 (= unit test
      `test_check_data_state_flags_populated_db_with_missing_import_jobs`
      で pin 済)。

## B. external access lockdown (= v0.5.1 主役 2/2)

`run_custom_query` 経由で外部 fs / network access が rejected
されること。 v0.5.0 adversarial で発覚した SSRF / 任意 fs 読み
取り経路を engine 設定で封鎖済。 既存 18 read tool には影響ゼロ
(= importer も含めて PyArrow register 経路で in-DB relation のみ
touch する設計)。

### B1. validator denylist 経由の reject (parse 時)

- [ ] **B1.1** `run_custom_query("SELECT * FROM read_csv('/etc/passwd')")`
      → `{"error":"...","details":"Function 'read_csv' is not
      allowed..."}` (= parse 時の denylist reject)。
- [ ] **B1.2** v0.5.1 で追加された alias:
      - `parquet_scan('/etc/passwd')`
      - `parquet_metadata('/etc/passwd')`
      - `parquet_schema('/etc/passwd')`
      - `sniff_csv('/etc/passwd')`
      
      いずれも parse 時に `Function 'X' is not allowed` で reject。

### B2. engine-level reject (= validator bypass しても engine が refuse)

validator が許可してもエンジン側 setting `enable_external_access =
false` で refuse される設計の確認。 通常の MCP client では validator
を bypass できないので、 ここは「設定が effective か」 を probe。

- [ ] **B2.1** `run_custom_query("SELECT current_setting('enable_external_access')")`
      → 結果が `"false"` (= 文字列)。
- [ ] **B2.2** `run_custom_query("ATTACH '/tmp/attacker.db' AS f")`
      → DDL で reject (validator)、 もしくは engine の Permission
      Error。 いずれにせよ ATTACH 不可。

### B3. https / s3 URL の egress 不可

- [ ] **B3.1** `run_custom_query("SELECT * FROM parquet_scan('https://raw.githubusercontent.com/duckdb/duckdb/main/README.md')")`
      → denylist reject (B1.2 と同じ branch を踏む) もしくは validator
      bypass しても engine Permission Error。 結果として external
      URL を fetch しない (= 「全データはローカル」 contract を engine
      レベルで担保)。

### B4. 通常の集計 SQL は壊れない (= run_custom_query 回帰)

- [ ] **B4.1** `run_custom_query("SELECT COUNT(*) FROM records")` →
      数値が返る。
- [ ] **B4.2** `run_custom_query("SELECT record_type, COUNT(*) FROM
      records GROUP BY 1 ORDER BY 2 DESC LIMIT 10")` → 集計が返る。
- [ ] **B4.3** `run_custom_query("SELECT * FROM imports")` → DB 列を
      含む結果 (= `duration_secs` 等。 wire 形は alias 後の
      `processing_secs` ではなく、 imports table の生 column)。

## C. wire-shape adjustments (= 副題 / #187 + #189 + #191)

### C1. list_zips hint が async polling 文言

- [ ] **C1.1** `list_zips` の `hint` フィールド (populated dir) を
      確認 → 「Pick an entry by id and call import_zip(id=…). ... a
      `job_id` ... poll `get_import_status(job_id=...)` every 10-30
      seconds ...」 系。 v0.4 文言 (`Claude will wait synchronously`)
      が**含まれない**。
- [ ] **C1.2** `imported=true` の entry に対する分岐説明が hint に
      載ってる (= 「imported=true なら同期 ok envelope、 job_id なし、
      polling しない」 が文中に出る)。
- [ ] **C1.3** 「elapsed_secs が 10 分超で stall を疑え」 系の閾値
      cue が hint にある (= worst-case 上限の代替)。

### C2. get_import_history.processing_secs alias

- [ ] **C2.1** `get_import_history` 結果の column 名:
      `import_id, export_dir, imported_at, record_count, workout_count,
      processing_secs, export_xml_sha256, records_after_dedup,
      dedup_skipped, source_zip_sha256, source_zip_mtime,
      source_zip_size`。 旧 `duration_secs` は wire shape から消えてる。
- [ ] **C2.2** 同 import_id の `get_import_status(job_id=...)` で
      返る `duration_secs` (= 全 worker wall-clock incl. ZIP extract)
      と `get_import_history.processing_secs` (= run_import body
      wall-clock のみ) で値が**異なる** (= 数秒差) ことを 1 つの
      import について実測。 → 集客 narrative の「ms で返る」 とは別軸
      の「ユーザ体感時間と本体スループットを別建てに見せる」 改善。
- [ ] **C2.3** `run_custom_query("SELECT duration_secs FROM imports
      LIMIT 1")` → DB 列名は据え置きで通る (= Layer 2 escape hatch
      無影響)。

### C3. import_zip id 寛容仕様

- [ ] **C3.1** `import_zip(id="6169BBD8")` (= 大文字 hex) → 受理
      されて `queued` envelope。 canonical `id` フィールド (= response)
      は lowercase 8 char。
- [ ] **C3.2** `import_zip(id="  6169bbd8  ")` (= 前後空白) → 受理
      されて A1 と同じ envelope。
- [ ] **C3.3** `import_zip(id="zzzz")` (= 非 hex) → `invalid_id`
      envelope。 error message に「case-insensitive, surrounding
      whitespace ignored」 系の表記がある。

## D. 既存 21 read tools / write tools の regression

import 完了後の fresh v0.5.1 DB に対して全 tool を網羅的に叩く。
v0.5.1 で touch した path (`get_import_history` の SELECT 形)
以外も含めて回帰チェック。

- [ ] **D1** `list_record_types` → record_type 一覧
- [ ] **D2** `query_records(record_type="HKQuantityTypeIdentifierHeartRate")`
- [ ] **D3** `get_record_statistics`
- [ ] **D4** `list_workouts`
- [ ] **D5** `get_workout_details(workout_hash=...)`
- [ ] **D6** `get_activity_summaries`
- [ ] **D7** `get_workout_route(workout_hash=..., with_heart_rate=true)`
- [ ] **D8** `get_heart_rate_samples(record_hash=...)`
- [ ] **D9** `list_correlations`
- [ ] **D10** `get_correlation_details`
- [ ] **D11** `list_ecg_readings`
- [ ] **D12** `get_ecg_data(ecg_hash=...)`
- [ ] **D13** `run_custom_query("SELECT COUNT(*) FROM records")`
- [ ] **D14** `list_data_sources`
- [ ] **D15** `get_import_history` → C2.1 で確認した wire shape
- [ ] **D16** `list_state_of_mind`
- [ ] **D17** `get_me_attributes`
- [ ] **D18** `get_server_info` → `version="0.5.1"`, record_count > 0
- [ ] **D19** `list_zips` → C1 系の hint 形
- [ ] **D20** `import_zip(id=<既 import 済 sha>)` → idempotent ok
      (= records_added: 0, already_imported_at populated)
- [ ] **D21** `get_import_status(job_id=<完走済 job>)` → 永続 ok
      envelope

## E. MCPB bundle 経路 (Claude Desktop)

Claude Code (CLI) で上記が全部通った後、 Claude Desktop で同シナリオを
小さく繰り返す。 lockdown が MCPB bundle 経路でも effective か確認。

- [ ] **E1** Settings → Extensions → 「Apple Health」 の
      `APPLE_HEALTH_EXPORT_ZIPS_DIR` に実 path を設定。
- [ ] **E2** chat で `list_zips` 系 prompt → tool 呼出し → C1 系の
      hint が返る。
- [ ] **E3** `run_custom_query("SELECT * FROM parquet_scan('/etc/passwd')")`
      系の試行を agent 経由で投げて denylist reject されるのを確認
      (= B1.2 の MCPB 経由版)。
- [ ] **E4** schema_outdated 経路 (= 0.4.legacy DB を `APPLE_HEALTH_DB`
      に向けた状態) で chat 開始 → `list_zips` の最初の呼出しで
      typed envelope が返り、 「`health.duckdb` を捨ててね」 という
      raw error ではなく「`import_zip` を呼べ」 系の人間文が agent
      に渡る。

## F. 失敗シナリオ (= 例外パス、 v0.5.0 から無回帰)

- [ ] **F1** 壊れた ZIP (= HTML を `.zip` リネーム) → `invalid_zip`
- [ ] **F2** Apple Health marker 無 ZIP → `not_apple_health_export`
- [ ] **F3** 存在しない id → `id_not_found`
- [ ] **F4** invalid id (非 hex) → `invalid_id` (= C3.3 と同じ envelope
      message を確認)
- [ ] **F5** `APPLE_HEALTH_EXPORT_ZIPS_DIR` 未設定で `import_zip` →
      `export_zips_dir_not_set`
- [ ] **F6** `get_import_status(job_id="ij_nope")` → `job_not_found`

## X. 7 人の意地悪な QA (adversarial dogfood)

[元ネタ](https://zenn.dev/nexta_/articles/be13a2395a5d2a) の「7 人の
意地悪な QA」 形式を踏襲。 ペルソナごとに「疑う点」 を切り替えて
攻めることで、 観点偏りと skip 誘惑を構造的に避ける。

v0.5.0 dogfood では別ファイル (`tmp/v0-5-0-adversarial-results_1.md`)
で flat な観点リストとして adversarial review を実施し、 そこで
stop-ship #190 (`parquet_scan` / `sniff_csv` 経由の SSRF + 任意 fs
読み取り) を発掘した。 v0.5.1 ではペルソナ駆動に切り替え、 test plan
本体に統合する。

**この章は実施者が「やりたくない」 と思いがちな観点を意図的に集めて
いる**。 環境用意が面倒・結果が読みづらい・心理的に億劫・再現困難
等で skip 誘惑が出るが、 v0.5.0 で stop-ship を 1 件発掘した実績
あり。

### 実施規律 (= 7 ペルソナ共通)

- **ペルソナを 1 人ずつ完全に演じる**。 P1 の途中で P3 のテストを
  混ぜない。 観点偏りが消えなくなる。
- **Test Basis (= 根拠) を必ず引用**。「issue #N の acceptance
  criteria」 「PR #N の CHANGELOG 行」 「README L96」 等、 一次情報
  との突合先を結果ログに残す。 推測ベースの不具合報告は除外。
- **未確認は「未確認」 と書く**。「※要 sub-process script (未実施)」
  等。 抜けを埋め草で塗らない。
- **修正判断はピコ (人間)**。 QA は発掘して報告するだけ。 修正方針
  決定は v0.5.0 #190 同様に grill / issue で別途。

### P1. 新人 user (= Apple Health を MCP 経由で初めて触る人)

**疑う点**: 説明書を読まず直感で agent に話す。 list_zips / import_zip
の概念が分からないまま「ヘルスデータ見て」 「グラフ作って」 と
言う。 誤クリック・誤 prompt・連発・前提省略。

**Test Basis**: README.md 「Installation」 章の前提読まずに開始する
user 像 — agent (= Claude Desktop) が hint / DESCRIPTION だけ頼り
に user を recovery 経路に誘導できることを v0.5.1 #187 で改善した。

- [ ] **P1.1** 何の前提もなく chat で「ヘルスデータの傾向見せて」 →
      agent が `list_zips` → ZIP 無/未設定 envelope を見て、 user に
      「export ZIP を `~/...` に置いて」 と誘導するか。 raw error が
      出ない。
- [ ] **P1.2** `APPLE_HEALTH_EXPORT_ZIPS_DIR` 未設定の状態で
      「今日の歩数」 を聞く → agent は read tool 試行 → NEEDS_CONFIG
      envelope → user に env 設定を指示。
- [ ] **P1.3** export.zip を間違って `Downloads/` に置いたまま
      「import して」 → agent が `list_zips` で「ZIP 無」 を見て
      「dir の中見たけど export.zip 無いよ、 どこにある?」 と user
      に聞き返す。
- [ ] **P1.4** **list_zips の hint をそのまま信じて操作する agent**
      (= #187 で書き換えた hint が誤誘導しない検証): hint 中の
      「Already-imported ZIPs (imported=true) short-circuit
      synchronously ... no `job_id`」 を agent が読んで、 imported=true
      の entry に対して polling を**しない**こと。 hint
      内容を agent が信じて polling 暴走しない。
- [ ] **P1.5** import 中に user が「あー、 やっぱり違う zip だった、
      キャンセル」 と発話 → agent が `get_import_status` で進捗を
      見せつつ、 cancel する API は無いので「次の起動時に orphan
      sweep で処理される、 待つしかない」 と説明できるか。
      cancel 経路は v0.5.x に無い (= 既知)。
- [ ] **P1.6** user が `import_zip` の結果を待たずに同セッションで
      「グラフ作って」 を連発 → agent が job_id を覚えていて
      `get_import_status` で polling、 終わるまで read tool は
      NEEDS_IMPORT envelope を返すこと。

### P2. データオタク (= `run_custom_query` を限界まで使う user)

**疑う点**: SQL を直書きして DB を query しまくる。 大量取得・複雑
集計・LIMIT 省略・OFFSET 連発で server を疲弊させる。 v0.5.1 #190
で `run_custom_query` の external access を engine lockdown 済み
だが、 健全な SQL 用途には影響ゼロな保証が必要。

**Test Basis**: CHANGELOG v0.5.1 `### Security` + `### Changed`
セクション、 issue #190 「Run on in-DB relations only」 contract、
README L20-29 「DuckDB-backed」 + Security 文言。

- [ ] **P2.1** 1 行に 100 万件の HeartRate を fetch する SQL: `SELECT
      string_agg(record_hash, ',') FROM records WHERE record_type =
      'HKQuantityTypeIdentifierHeartRate'` → `Tool result is too
      large` ハードエラー (= v0.5.0 dogfood で確認した持病解消の
      無回帰)。
- [ ] **P2.2** LIMIT 無しで HeartRate 全件 `SELECT * FROM records
      WHERE record_type='...'` → `row_count: 1000` / `truncated:
      true` (= 行数 cap、 v0.5.0 確認済の無言切り解消の無回帰)。
- [ ] **P2.3** WINDOW 関数 / 再帰 CTE 等の高度 SQL を投げる:
      `WITH RECURSIVE t(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM t
      WHERE n < 1000) SELECT COUNT(*) FROM t` → 1000 返る (= server
      の SQL processing は完全動作)。
- [ ] **P2.4** GROUP BY + window function 複合: `SELECT record_type,
      AVG(value) OVER (PARTITION BY DATE(start_date)) FROM records
      LIMIT 100` → 集計結果。
- [ ] **P2.5** **同じ query を 100 連発** (agent prompt で「100 回
      query して」 系) → server 落ちず、 各 response 取り出せる。
      lock 競合観察。
- [ ] **P2.6** OFFSET 100,000 / LIMIT 100 → 大量 OFFSET でも crash
      せず、 適切に rows 返却 or 空 list。

### P3. 悪意ある操作者 (= SQL injection / path / 巨大入力)

**疑う点**: 境界値・不正値・権限外。 SQL injection、 path traversal、
NULL byte、 巨大入力、 Unicode 攻撃。 v0.5.1 #190 で発掘した stop-ship
SSRF の retest。

**Test Basis**: `tmp/v0-5-0-adversarial-results.md` + `_1.md` の §2-2
「バイパス発見」 を再叩き、 PR #200 で fix した contract が effective
か。 issue #190 acceptance criteria「parquet_scan('https://...') が
engine 経由で reject される」。

### X1. SQL safety 無回帰 (v0.5.0 で発覚した穴の retest)

- [ ] **X1.1** `run_custom_query("SELECT * FROM parquet_scan('/etc/passwd')")`
      → `Function 'parquet_scan' is not allowed` (denylist) もしくは
      engine `Permission Error`。
- [ ] **X1.2** `parquet_metadata` / `parquet_schema` / `sniff_csv`
      も同様に reject。
- [ ] **X1.3** `run_custom_query("WITH x AS (SELECT * FROM
      read_text('/etc/passwd')) SELECT * FROM x")` → CTE 内に隠した
      denylist 関数も reject (= validator が再帰チェック)。
- [ ] **X1.4** `run_custom_query("SELECT * FROM parquet_scan(
      'https://raw.githubusercontent.com/duckdb/duckdb/main/README.md')")`
      → denylist or engine で reject (= URL egress 不可)。
- [ ] **X1.5** `run_custom_query("ATTACH 'https://attacker.example/x.db'
      AS f")` → URL-backed ATTACH も reject。
- [ ] **X1.6** `run_custom_query("SELECT * FROM duckdb_settings()")`
      → 通る (= 既知の by-design 抜け、 #190 で「allowlist 反転却下に
      伴う未対応」 として保留)。 `temp_directory` 等の path が見える
      が、 同じ path は `get_server_info` で既に見せてるため低リスク。
      ここでは「無回帰」 を確認するだけ (= 通る挙動が変わってない)。
- [ ] **X1.7** `run_custom_query("SELECT * FROM records; DROP TABLE
      records;")` → 複文 reject (= `Only a single SQL statement`)。
- [ ] **X1.8** `run_custom_query("COPY records TO '/tmp/out.csv'")`
      → COPY reject。

### X2. 入力境界 (空 / null / 巨大 / NULL byte / Unicode)

- [ ] **X2.1** `import_zip(id="")` → `invalid_id`。
- [ ] **X2.2** `import_zip(id="a")` (= 4 char 未満) → `invalid_id`。
- [ ] **X2.3** `import_zip(id="a"*65)` (= 64 char 超) → `invalid_id`。
- [ ] **X2.4** `import_zip(id="6169bbd8 malicious")` (= NULL
      byte 注入) → `invalid_id` (= hex 以外を含むため)。 server
      クラッシュしない。
- [ ] **X2.5** `import_zip(id="ｆｆｃ７２ａ０ｆ")` (= 全角 hex) →
      `invalid_id`。
- [ ] **X2.6** `query_records(limit=0)` → `limit must be >= 1`。
- [ ] **X2.7** `query_records(limit=999999)` → 1000 に clamp、
      `next_offset: 1000` 等で truncated state を明示。
- [ ] **X2.8** `query_records(offset=-1)` → 先頭ページ (0 扱い) or
      validation error、 いずれにせよ server クラッシュなし。
- [ ] **X2.9** `query_records(start_date="2020-01-01' OR '1'='1")`
      → `Conversion Error` (= SQL injection 不可、 timestamp cast で
      文字列連結禁止)。
- [ ] **X2.10** `query_records(start_date="2050-13-45")` (= 不正
      日付) → `Conversion Error`。
- [ ] **X2.11** `query_records(start_date="2030-01-01",
      end_date="2020-01-01")` (= start > end) → `[]` (= 空)、
      error にせず 0 行返し。

### X3. Path / 環境変数の意地悪

- [ ] **X3.1** `APPLE_HEALTH_EXPORT_ZIPS_DIR=../../../etc` (= relative
      で directory escape) → 中身が apple health zip じゃないので
      `list_zips` は zip なしの hint envelope (= `Drop your Apple
      Health export.zip ...`)。 directory escape 自体は server 側で
      明示禁止しないが、 zip 検査で篩落とされる。
- [ ] **X3.2** `APPLE_HEALTH_EXPORT_ZIPS_DIR=C:\Windows` → 同上。
- [ ] **X3.3** `APPLE_HEALTH_EXPORT_ZIPS_DIR` 未設定で `list_zips` /
      `import_zip` 叩く → 既存 F5 確認 (= `export_zips_dir_not_set`)。
- [ ] **X3.4** `APPLE_HEALTH_DB=/etc/passwd` (= 既存ファイルに書き
      込み試行) → server 起動時に Permission Error or
      ConfigError、 起動中なら次の write tool 呼出し時。 raw stack
      trace ではなく typed error が出ること。
- [ ] **X3.5** `APPLE_HEALTH_DB=con` (= Windows 予約名) → 起動エラー
      系の typed error、 server クラッシュではない。 Windows でのみ
      意味あるので zion で実施。
- [ ] **X3.6** `APPLE_HEALTH_DB=` (= 空文字) → server が
      `resolve_db_path` の XDG default に fallback、 起動成功
      (= 既存 v0.3.0 #133 仕様の再確認)。

### X4. ZIP の意地悪

- [ ] **X4.1** **空 ZIP** (0 byte file) を export dir に置く →
      `list_zips` で `zip_status: invalid_zip`、 `import_zip(id=...)`
      で `invalid_zip` envelope。
- [ ] **X4.2** **1 byte ZIP** (= ZIP 仕様より短い) → 同上。
- [ ] **X4.3** **マルチエントリ ZIP** で `apple_health_export/export.xml`
      が含まれてるが 0 byte → import 完走するが record_count=0、
      server クラッシュなし。
- [ ] **X4.4** **zip slip 攻撃**: ZIP 内に `../../../../tmp/escape.txt`
      の path entry を含む ZIP → importer が zip path を unsafely
      展開してないか確認。 `zip_extract.py` の `extract_zip_and_import`
      実装次第。 期待: zip slip path は無視 or extraction error 系。
- [ ] **X4.5** **同 sha256 だが file_name 違う 2 個の ZIP** を export
      dir に並べる → `list_zips` で 2 entries 両方 listed、 1 個目を
      `import_zip` した後 2 個目を叩くと idempotent ok envelope
      (= sha cache hit)。
- [ ] **X4.6** mtime が未来 (= 2100 年) の ZIP → `list_zips` で mtime
      ISO 表示、 cache key (size, mtime) としては問題なし。 server
      クラッシュなし。
- [ ] **X4.7** ZIP 内 `export.xml` に**意図的に壊れた XML** (= 閉じ
      タグ漏れ) → Phase-1 で parse error、 typed error envelope
      (= `run_import_failed` 系) で job が `error` 終端。
- [ ] **X4.8** **巨大 export.xml** (= 5GB 級は環境負荷大、 任意で
      skip 可)。 zion / hal 環境次第。

### X5. Job 状態マトリクス

- [ ] **X5.1** 完走済 `job_id` (= status='ok') を再度 `get_import_status`
      → 永続した ok envelope を再返却 (= 冪等 read)。
- [ ] **X5.2** **正しい形式の偽 `job_id`** (例
      `ij_20260629_999999_zzzzzzzz_zzzz`) → `job_not_found`。
- [ ] **X5.3** **不正形式 `job_id`** (`ij_nope` / `abc` / 空文字) →
      `job_not_found` (= F6 既存確認)。
- [ ] **X5.4** **別 sha の job_id** を別 session の job_id として
      使い回す → `job_not_found` (= cross-session で永続性確認、
      v0.5 では import_jobs テーブルに残るので別 session でも見える
      想定)。

### X6. 並列 / Race (= 任意 / sub-process script 要)

agent ループ単独では sub-second の同時 2 連発を再現できない。
v0.5.0 dogfood でも未踏のまま skip した経緯あり、 sub-process script
を別途用意できれば踏む。 unit test (`test_concurrent_import_zip_*`
系) で構造は pin 済。

- [ ] **X6.1** (任意) 同 sha を 2 連発 (Python script で
      `subprocess.Popen` 2 並列 or `asyncio.gather`) → 2 回目も
      `queued` envelope だが `job_id` が 1 回目と**同一**。
- [ ] **X6.2** (任意) import 走行中に DB ファイルを別プロセスから
      `rm` → server が crash しないか。 DuckDB 仕様次第で IO Error
      で job が `error` 終端、 server プロセスは生存。
- [ ] **X6.3** import 走行中に Claude Desktop を強制終了 →
      再起動 → orphan sweep が走って前 job が
      `server_restarted_while_running` envelope で終端 (= B3 既存
      確認の再現)。

### X7. schema_outdated escape hatch

v=5 DB に対して `run_custom_query` 経由で imports / import_jobs を
直叩きした時の挙動。 `run_custom_query` には schema_outdated gate が
**意図的に無い** (= SQL escape hatch であり、 raw error も含めて user
の責任) 設計。 ここでは「意図通り escape hatch として動く」 ことを
確認。

- [ ] **X7.1** (legacy v=5 DB を `APPLE_HEALTH_DB` に向けた状態で)
      `run_custom_query("SELECT COUNT(*) FROM imports")` → 通る
      (= imports table は v=5 にも存在)。
- [ ] **X7.2** 同状態で `run_custom_query("SELECT COUNT(*) FROM
      import_jobs")` → raw `Catalog Error: Table import_jobs does
      not exist`。 これは by-design carve-out (= run_custom_query
      は agent / user 責任の escape hatch、 typed envelope に
      包まれない)。 期待動作。
- [ ] **X7.3** 同状態で `list_zips` → schema_outdated envelope
      (= A1.1 既存確認)、 X7.2 と並べて「typed envelope path vs raw
      SQL escape hatch」 の対比が明確。

### X8. 破壊耐性 final 確認

X1-X7 全試行後に以下を確認。 v0.5.0 adversarial の §7 と同じ規律。

- [ ] **X8.1** `run_custom_query("SELECT COUNT(*) FROM records")` →
      X 章開始前と同じ件数 (= 破壊系試行で 1 行も削れてない)。
- [ ] **X8.2** `run_custom_query("SELECT COUNT(*) FROM imports")` →
      X 章で完走した import 分の増加のみ (= 既存 import row 無傷)。
- [ ] **X8.3** `run_custom_query("SELECT COUNT(*) FROM import_jobs")`
      → 完走 + error 終端含めた合計、 prefix `ij_...` 形式が崩れた
      row なし。
- [ ] **X8.4** `run_custom_query("SELECT COUNT(*) FROM duckdb_tables()
      WHERE schema_name='main'")` → テーブル数 21 (= v=6 schema 完全
      保持)。
- [ ] **X8.5** `get_server_info` → version="0.5.1", record_count =
      X8.1 と一致、 db_path = 期待値。



v0.5.0 で取り込んだ DB (= 既に `schema_version=6`、 `import_jobs`
存在) を v0.5.1 server で開く経路。 同 schema version なので
no-op 想定。

- [ ] **G1** v0.5.0 で取り込んだ DB を `APPLE_HEALTH_DB` に向けて
      v0.5.1 server 起動 → 起動エラー無し。
- [ ] **G2** `get_server_info` で `version="0.5.1"` 確認、 既存
      record_count はそのまま。
- [ ] **G3** D の read tool 群を抜き打ちで叩く → 既存データそのまま
      query できる (= migration 不要)。

## H. 集客 narrative の補強

v0.5.0 で `"Runs on whatever you have: Apple Silicon laptop, $300
N100 mini PC, anything in between."` を固定化済。 v0.5.1 では
narrative 軸は変えず、 **security と agent UX 改善** を裏側で語れる
ように。 集客投稿時に直接コピーで使う想定ではない (= H 章は
narrative 拡張用素材集め)。

- [ ] **H1** B 章の結果を 1 文に: 「SQL escape hatch (`run_custom_query`)
      operates only over in-DB relations; the engine refuses every
      fs / network function at the connection level.」 程度。
- [ ] **H2** A 章の結果を 1 文に: 「Opening an older alpha DB returns
      a typed `schema_outdated` envelope instead of a raw DuckDB
      error — agents get a single recovery hint, users do not need a
      terminal.」 程度。
- [ ] **H3** C2 の数値 pin を 1 つ: 「`processing_secs` (= import
      body) vs `duration_secs` (= queue→done worker wall-clock incl.
      ZIP extract) の差は実測 N 秒」 → 集客 narrative の F&Q 素材
      (= 「なんで 2 つの数字がある?」 の回答に使う)。

## 終了条件

**A〜F + X (X1-X5 + X7-X8 必須、 X6 は任意)** が all green になったら
v0.5.1 の集客フェーズ復帰可。 G は v0.5.0 DB を保持してれば確認、
持ってない場合 skip 可。 H は集客 narrative 拡張時の素材集め
(= 必須ではない)。

X 章は「実施者が skip 誘惑を感じやすい」 章なので意識的に通すこと。
v0.5.0 では adversarial で stop-ship #190 (`parquet_scan` SSRF) を
1 件発掘した実績あり。 退屈・面倒・心理的に億劫でも 1 個飛ばすと
発掘漏れの母体になる。

dogfood で発見した defect は GitHub issue 起票 (`type:fix` /
`stop-ship` ラベル) → 当該 fix を **v0.5.2 hot-fix** で出してから
集客、 が筋。 v0.5.2 milestone は既に #193 (README rot) が乗ってる
ので、 defect 起票時はそこに追加するのが流れ。 stop-ship 系は新規
milestone 切るかピコ判断。

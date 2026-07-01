# v0.5.0 dogfood test plan

v0.5.0 の主役 = `import_zip` の job-based async 化 + 新 `get_import_status`。
集客 narrative (= 環境スペック比例で同期実装が破綻していたのを async で解決)
の裏取りが目的。 v0.3.0-rc2 の流儀
(`docs/dogfood/v0-3-0-rc2-test-plan.md`) を踏襲。

## 0. セットアップ

zion 物理操作前提。 順に消化。

- [ ] **0.1** Claude Code (CLI) で v0.5.0 を pin インストール:
      `uvx --from 'apple-health-mcp-server==0.5.0' apple-health-mcp-server --version`
      → `0.5.0` が表示される。 `@latest` だけだと `uvx` キャッシュで古い版が
      残る罠あり ([[reference-pypi-uvx-pre-release-at-latest-pitfall]]) のため
      `==0.5.0` 明示。
- [ ] **0.2** Claude Desktop (MCPB bundle):
      <https://github.com/rinoshiyo/apple-health-mcp-server/releases/tag/v0.5.0>
      から `.mcpb` を DL → ダブルクリックでインストール → Settings → Extensions
      に「Apple Health (0.5.0)」 表示。
- [ ] **0.3** `APPLE_HEALTH_EXPORT_ZIPS_DIR` を実 export 置き場 (例:
      `D:\data\OneDrive - itrans\apple-health-exports`) に設定。
- [ ] **0.4** `APPLE_HEALTH_DB` を一時用に空 path (= 別 DB ファイル) に設定
      して、 fresh 状態で dogfood を走らせる。 過去の v0.4.1 DB に
      ぶつけない (= v0.4.1 → v0.5.0 で schema_version_is_stale 経路の検証も
      別途要るが、 まずは fresh-import の happy path を確実に踏む)。

## A. import_zip の非同期化 (= v0.5.0 主役)

### A1. 基本 async フロー — happy path
- [ ] **A1.1** `list_zips` を叩く → 実 export.zip が `id` 8 char hex で
      列挙される。 `imported: false` であることを確認。
- [ ] **A1.2** `import_zip(id=<sha8>)` を叩く → **1 秒以内** に
      `{"status":"queued","job_id":"ij_YYYYMMDD_HHMMSS_<sha>_<rand>",
      "id":"<sha8>","queued_at":"...","message":"..."}` が返る。
      ← v0.4 では 44-200+ 秒同期だった箇所、 ms オーダーが集客 narrative の根拠。
- [ ] **A1.3** すぐに `get_import_status(job_id=...)` を polling:
      - 1 回目: `{"status":"queued"|"running","phase":"extracting"|...,
        "elapsed_secs":...}`
      - 30 秒間隔で叩いて `phase` が `extracting → xml_parsing → ecg → gpx
        → finalize` と推移する。
      - 最終: `{"status":"ok","id":"<sha8>","job_id":"...",
        "records_added":N,"workouts_added":M,...,"duration_secs":...,
        "already_imported_at":null,"message":"..."}` — `id` フィールドが
        ある (F4 fix)、 `duration_secs` が float、 stats が legacy
        sync envelope と同形。

### A2. phase 進捗
- [ ] **A2.1** import 中の polling で `phase` が実際に `extracting` 以外を
      返す瞬間がある (= phase_callback 配線が生きてる)。 mark_running 直後
      は `extracting`、 すぐに `xml_parsing` に切り替わるはず。
- [ ] **A2.2** v0.5.0 F1 fix の確認: zip 展開フェーズ中 (lock 外)、 他の
      read tool (例: `get_import_history`) を叩いても server がクラッシュ
      しない / 結果が壊れない。 → DuckDB cursor race のレグレッションが無い。

### A3. ms 応答の数値計測 (= 集客 narrative の裏付け)
- [ ] **A3.1** Claude Code (CLI) の tool-call ログから `import_zip` の
      要した wall-clock 時間を見る (返却までの ms)。 ターゲット: < 2000 ms
      (v0.5 spec の「returns in milliseconds」 contract)。
- [ ] **A3.2** 1.2 GB 級の実 export.zip で `import_zip` を初回 (sha cache
      miss) で叩く: F2 fix (asyncio.to_thread 復活) で `stream_sha256` が
      event loop を block しないこと。 client (Claude Desktop) が
      `import_zip` の return を待つ間に他 tool 反応を返すか。

## B. Idempotency & guards

### B1. 冪等 short-circuit (= byte-identical re-import)
- [ ] **B1.1** A1 で import 完了済の同 ZIP に対して再度 `import_zip(id=...)`
      を叩く → 同期で `{"status":"ok","id":...,"records_added":0,
      "already_imported_at":"<前回の imported_at>"}` が ms で返る。
      `job_id` が**含まれない**ことを確認 (= job 作成しない idempotent
      short-circuit)。
- [ ] **B1.2** `import_jobs` テーブルに B1.1 で新 row が**追加されない**ことを
      `run_custom_query('SELECT COUNT(*) FROM import_jobs')` で確認。

### B2. multi-launch guard (= 同 sha の同時 2 連発)
- [ ] **B2.1** import_zip(sha8) を叩く (worker spawn) → 即座にもう 1 回叩く。
      2 回目の return も `{"status":"queued","job_id":"ij_..."}` だが、
      **job_id が 1 回目と同一** (F3 fix: claim_or_get_active で atomic)。
- [ ] **B2.2** `SELECT COUNT(*) FROM import_jobs WHERE source_sha256=?` で
      行数 = 1 (= duplicate row が作られてない)。

### B3. orphan recovery (= server 再起動で worker が消えた状態)
- [ ] **B3.1** import_zip 叩いて worker 実行中の状態を作る。
- [ ] **B3.2** Claude Desktop の MCP server を kill (= Settings → Extensions
      → disable → re-enable、 もしくは Claude Desktop 自体を完全終了 → 再起動)。
- [ ] **B3.3** 再起動後すぐ `get_import_status(job_id=<前 worker の id>)`
      を叩く → `{"status":"error","reason":"server_restarted_while_running",
      "message":"Server restarted before the import worker completed."}`
      ← boot sweep が走った証。
- [ ] **B3.4** 同 sha で再度 `import_zip(id=...)` 叩く → 新 worker spawn
      (= 古い orphan job で guard が wedge してない)。

## C. Regression — 既存 18 read tools

import 完了後の DB に対して既存 tool を網羅的に叩く。 v0.4 dogfood の
test plan からツール一覧を引用、 v0.5 で touch してない tool でも回帰
チェックする。

- [ ] **C1** `list_record_types` → 既存 record_type が並ぶ
- [ ] **C2** `query_records(record_type="HKQuantityTypeIdentifierHeartRate")`
      → envelope 形 (items / total / next_offset) が pre-v0.5 と一致
- [ ] **C3** `get_record_statistics` → 既存通り
- [ ] **C4** `list_workouts` → envelope 形
- [ ] **C5** `get_workout_details(workout_hash=...)`
- [ ] **C6** `get_activity_summaries`
- [ ] **C7** `get_workout_route(workout_hash=..., with_heart_rate=true)`
      → v0.4 で追加された heart_rate join が生きてる
- [ ] **C8** `get_heart_rate_samples(record_hash=...)`
- [ ] **C9** `list_correlations`
- [ ] **C10** `get_correlation_details`
- [ ] **C11** `list_ecg_readings`
- [ ] **C12** `get_ecg_data(ecg_hash=...)`
- [ ] **C13** `run_custom_query("SELECT COUNT(*) FROM records")`
- [ ] **C14** `list_data_sources`
- [ ] **C15** `get_import_history` → 直近の import 行が見える
- [ ] **C16** `list_state_of_mind`
- [ ] **C17** `get_me_attributes` → date_of_birth 等
- [ ] **C18** `get_server_info` → version="0.5.0", record_count > 0,
      db_path = 期待値

## D. import 中の read tool 並行

worker 実行中の挙動を pin。 production の集客 narrative では「import 中でも
Claude が他の質問に答えられる」 を売る。

- [ ] **D1** `import_zip(id=...)` 叩いて worker 進行中。
- [ ] **D2** すぐに `get_import_history` を叩く → 結果が返ってくる (= writer
      lock を取らずに済む read tool)。 待たされる秒数も観察。
- [ ] **D3** `run_custom_query("SELECT 1")` → 即時返却 (writer lock 競合
      時のみ待つ仕様、 ピクセル的に困らない量か観察)。

## E. MCPB bundle 経路 (Claude Desktop)

Claude Code (CLI) で上記が全部通った後、 Claude Desktop で同シナリオを
小さく繰り返す。 主目的は MCPB bundle 経路で worker thread が daemon と
して立ち上がるか確認。

- [ ] **E1** Claude Desktop の Settings → Extensions → 「Apple Health」
      の `APPLE_HEALTH_EXPORT_ZIPS_DIR` に実 path を設定。
- [ ] **E2** Claude Desktop の chat で「list_zips してみて」 → tool 呼び
      出される。
- [ ] **E3** 「上の sha8 で import_zip して、 30 秒ごとに get_import_status
      で進捗を見せて」 → agent が polling 動作する (= description の
      polling 戦略誘導が効いてる)。
- [ ] **E4** import 完了後、 「records_added 教えて」 → `get_import_status`
      の `records_added` で答える。

## F. 失敗シナリオ (= 例外パス)

- [ ] **F1** 壊れた ZIP (= HTML を `.zip` リネーム) を export dir に置く →
      `list_zips` で `zip_status: invalid_zip`、 `import_zip(id=...)` で
      sync error `{"status":"error","reason":"invalid_zip"}`。
- [ ] **F2** valid だが Apple Health export marker なし ZIP → sync error
      `{"status":"error","reason":"not_apple_health_export"}`。
- [ ] **F3** 存在しない id → `{"status":"error","reason":"id_not_found"}`。
- [ ] **F4** invalid id (非 hex) → `{"status":"error","reason":"invalid_id"}`。
- [ ] **F5** `APPLE_HEALTH_EXPORT_ZIPS_DIR` 未設定で `import_zip` → sync
      error `{"status":"error","reason":"export_zips_dir_not_set"}`。
- [ ] **F6** `get_import_status(job_id="ij_nope")` → `{"status":"error",
      "reason":"job_not_found"}`。

## G. v0.4.1 → v0.5.0 マイグレーション

過去 dogfood で v0.4.1 DB を残しておいた場合のみ。

- [ ] **G1** 既存 v0.4.1 DB の path で v0.5.0 server を起動 → 起動エラーで
      ない (= schema_version_is_stale で fresh-reset → ensure_schema が
      `import_jobs` テーブルを作る)。
- [ ] **G2** `get_server_info` で `version="0.5.0"` 確認。
- [ ] **G3** A1 を再走 (実 ZIP で fresh-import) → ok envelope 着地。

## H. 集客 narrative の裏取り (= dogfood 完走後)

dogfood log を踏まえて、 Reddit / awesome-mcp / Anthropic Directory に
出す前に narrative を 1 文で固める:

- [ ] **H1** A3.1 で計測した「import_zip return までの ms」 を 1 つ pin
      (例: `~150ms regardless of export size`)。
- [ ] **H2** D2-D3 で計測した「import 中の他 tool の応答性」 を 1 文で
      pin (例: `read tools stay responsive during multi-minute imports`)。
- [ ] **H3** F の async error 経路を 1 文 (例: `failed imports surface
      a typed error envelope without crashing the server`)。

## 終了条件

A〜F が all green になったら集客フェーズ復帰可。 G は過去 DB を保持してれば
やる任意項目。 H は集客 narrative 作成時の素材。

dogfood で発見した defect は GitHub issue 起票 (`type:fix` ラベル) → 当該
fix を v0.5.1 hot-fix で出してから集客、 が筋。

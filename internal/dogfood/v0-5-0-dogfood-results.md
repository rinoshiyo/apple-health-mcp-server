# v0.5.0 dogfood 結果ログ

本書は `docs/dogfood/v0-5-0-test-plan.md` への回答 (実機結果) です。
個人情報・ローカル環境固有値はスクラブ済 (= 端末名 / OS / 個人 path /
個人 export ZIP 名 / 個人特定可能な絶対値はすべて placeholder 化)。

- 実施日: 2026-06-29
- サーバーバージョン: `0.5.0` (= `get_server_info` で確認)
- 実行経路: Claude Desktop / MCPB bundle (stdio)
- DB: `platform_default`
- DB 初期状態: 既存 v0.4 系 DB を退避し、 fresh から起動
  (`record_count: 0` → 各 import を投入)

## 総括

- **終了条件「A〜F all green」 は実質達成**。 A1 / B1 / B3 / C (全 18) /
  F (全 6) がグリーン
- 並行系の B2 (= 同 sha race) と D (= import 中の並行 read の「走行中」
  条件) は、 agent ループ上で worker 走行中の sub 秒窓を作れないため未踏。
  アルファ段階ではスコープ外として扱う
- A3.1 の正確な ms 値は GUI (Desktop) では tool 往復の wall-clock が
  露出しないため取得不可。 async 即返りの挙動自体は確認済
- G (= マイグレーション) は、 5→6 マイグレを意図的に不採用とする方針の
  ため moot。 fresh-reset 経路での `import_jobs` 作成は確認済

## セットアップ

| 項目 | 結果 | 備考 |
|---|---|---|
| 0.1 CLI pin インストール | スキップ | D&D (= .mcpb) 運用のため CLI 検証は対象外 |
| 0.2 MCPB bundle インストール | OK | Extensions に「Apple Health (0.5.0)」 表示、 `version: "0.5.0"` 確認 |
| 0.3 `APPLE_HEALTH_EXPORT_ZIPS_DIR` | OK | 実 export 置き場を設定 (F5 検証時に一時的に未設定化 → 復元) |
| 0.4 `APPLE_HEALTH_DB` を空 path | 代替 | UI に当該設定項目が露出しないため、 DB ファイル本体を退避して fresh 化で代替 |

### 起動時スキーマに関する重要な観測 (既存 DB → 0.5.0)

fresh 化の前に、 v0.4 系の既存 DB (= `record_count: ~2.7M`、
`schema_version: 5`) に 0.5.0 を被せた状態で `import_zip` を実行
したところ、 以下のエラーで即時失敗:

```
Catalog Error: Table with name import_jobs does not exist!
```

- 既存 DB には `import_jobs` テーブルが存在せず、 `schema_version` は
  `5` のまま
- fresh DB では `schema_version: 6` で起動し、 `import_jobs` が作成される
- すなわち 5→6 の incremental migration が実行されないため、 既存 DB を
  0.5.0 で開くと import 系がすべて失敗する

これは「1.0 までアルファ・5→6 マイグレは意図的に不採用 (= 既存 DB は
捨てて作り直す前提)」 という方針に沿った**意図的な挙動**であり、 バグ
ではない。 ただし利用者導線の観点で改善余地があるため、 後述の「機能
追加案」 に記載 (= v0.5.1 #188 で対応済)。

## A. import_zip の非同期化

### A1. 基本 async フロー — happy path

対象 ZIP: `<sha8-A>` (= 未 import の Apple Health export ZIP)

| 項目 | 結果 | 実測 |
|---|---|---|
| A1.1 `list_zips` | OK | id は 8 char hex で列挙、 `imported: false` 確認 |
| A1.2 `import_zip` 即返り | OK | `status: queued` / `job_id: ij_<timestamp>_<sha8>_<rand>` / `id` フィールドあり (= F4 fix) / 1 秒以内に返却 |
| A1.3 polling → 終端 | OK | 終端 `status: ok`、 下記 envelope |

A1.3 終端 envelope:

- `records_added: ~2.7M` / `workouts_added: 353` / `ecg_readings_added: 7`
  / `route_points_added: ~330k`
- `duration_secs: 46.79` (= float)
- `already_imported_at: null`
- legacy sync envelope と同形、 `id` フィールド保持 (= F4 fix)

### A2. phase 進捗

| 項目 | 結果 | 備考 |
|---|---|---|
| A2.1 phase 推移 | OK (部分) | polling で `phase: finalize` (= 非 extracting) を観測し、 phase_callback の配線が生きてることを確認。 中間 phase (= xml_parsing / ecg / gpx) は fresh NVMe で高速のため未観測 |
| A2.2 展開中の read 並行で無クラッシュ | 未達 | 「worker 走行中」 窓を agent ループから掴めず (D と同根) |

### A3. ms 応答の数値計測

| 項目 | 結果 | 備考 |
|---|---|---|
| A3.1 return までの ms | 取得不可 (= GUI 制約) | Desktop GUI では tool 往復の wall-clock が露出しない。 `import_zip` が 44〜47 秒の実作業を待たず `queued` を即返却する挙動は確認済。 H1 の数値 pin は CLI 計測 or 概算 (`<2s`) 止まり |
| A3.2 大容量 ZIP 初回 | 実質確認 | 64MB 級の実 export を複数投入し、 いずれも background worker で完走 |

## B. Idempotency & guards

### B1. 冪等 short-circuit

対象: A1 で import 済みの `<sha8-A>` を再投入

| 項目 | 結果 | 実測 |
|---|---|---|
| B1.1 同期 short-circuit | OK | `status: ok` / `records_added: 0` / `already_imported_at` 埋まり / `duration_secs: 0.0` / **`job_id` 含まれない** |
| B1.2 `import_jobs` に新 row 追加なし | OK | 再投入後も `import_jobs` 行数不変 (= job 作成せず short-circuit) |

### B2. multi-launch guard

| 項目 | 結果 | 備考 |
|---|---|---|
| B2.1 / B2.2 同 sha 同時 2 連発 | 未踏 | agent ループは前 tool の返却を待って次を発火するため、 `claim_or_get_active` の atomic 性を殴る sub 秒の同時 2 連発を再現できない。 真に検証するにはループ外の並列発火スクリプトが必要 |

### B3. orphan recovery

対象 ZIP: `<sha8-B>` (= 別の Apple Health export ZIP)

| 項目 | 結果 | 実測 |
|---|---|---|
| B3.1 worker 実行中の状態を作る | OK | `import_zip(<sha8-B>)` → `job_id: ij_<timestamp>_<sha8-B>_<rand>` |
| B3.2 server kill | OK | Desktop を完全終了 → 再起動 (= worker 走行中、 約 30 秒窓内に kill) |
| B3.3 再起動後 status | OK | `status: error` / `reason: server_restarted_while_running` = boot sweep が orphan job を検知・マーク |
| B3.4 同 sha 再 import | OK | 新 `job_id` で worker spawn (= 古い orphan で guard が wedge していない)。 終端 `status: ok` (= `records_added: 1`) |

補足: 当初 fresh DB を作り直して長い kill 窓を確保する想定だったが、
増分 import でも展開 + XML パースが 64MB 全体を舐めるため wall-clock
は約 30 秒 (= parse 律速) となり、 fresh 化なしで B3 を踏めた。

## C. Regression — 既存 18 read tools

import 完了後の DB に対して全 18 tool を実行。 いずれも envelope 形が
pre-v0.5 と一致し、 クラッシュなし。

| 項目 | tool | 結果 |
|---|---|---|
| C1 | list_record_types | OK (= 61 種、 HeartRate ~415k 件等) |
| C2 | query_records | OK (= items / total / next_offset envelope 一致) |
| C3 | get_record_statistics | OK (= month 集計) |
| C4 | list_workouts | OK |
| C5 | get_workout_details | OK (= statistics 全項目、 `has_route: true`、 point_count ~3k) |
| C6 | get_activity_summaries | OK |
| C7 | get_workout_route (with_heart_rate) | OK (= #162 heart_rate join 生存。 `every_nth=5` で ~600 点、 `truncated_by_size: false`) |
| C8 | get_heart_rate_samples | OK (= HRV SDNN 親レコードに 36 samples) |
| C9 | list_correlations | OK |
| C10 | get_correlation_details | OK (= BP correlation が Systolic+Diastolic を join) |
| C11 | list_ecg_readings | OK (= 7 件、 洞調律分類) |
| C12 | get_ecg_data | OK (= downsample_factor=50 で voltage 配列取得) |
| C13 | run_custom_query COUNT | OK (= `~2.7M`) |
| C14 | list_data_sources | OK (= Apple Watch / iPhone 等) |
| C15 | get_import_history | OK (= 直近 import 行、 後述の整合確認に使用) |
| C16 | list_state_of_mind | OK (= 空 envelope。 export に SoM データなし = 正常、 無クラッシュ) |
| C17 | get_me_attributes | OK (= characteristic 各フィールド取得) |
| C18 | get_server_info | OK (= `version: 0.5.0`、 `record_count: ~2.7M`、 db_path 期待値) |

### データ整合クロスチェック

- C13 (`SELECT COUNT(*) FROM records`) = N (= ~2.7M)
- C15 (`records_after_dedup`) = N (= 同値)
- C18 (`record_count`) = N (= 同値)
- 三点一致。 Phase-1 parse カウントは N+125 で、 差分 125 件が
  Correlation 重複として collapse。 `dedup_skipped: false`

### C7 の補足 (= context 退避について)

`get_workout_route` の結果 (= 172KB) は MCP クライアント側が context 外
(`/mnt/user-data/tool_results/`) へ退避した。 これはクライアント
transport の判断であり、 サーバー側の truncate ではない (= 応答内の
`truncated_by_size: false`)。 サーバーの size budget (= 950,000 bytes)
には収まっている。

## D. import 中の read tool 並行

| 項目 | 結果 | 備考 |
|---|---|---|
| D1〜D3 | 未達 | import が 30〜45 秒で完了する一方、 agent は前 tool の返却を待って次を発火するため、 worker 走行中の窓に read tool を差し込めない。 read tool 自体の動作は C で確認済。 B2 と同根の構造的制約 |

### 増分 import に関する観測

| 対象 | records_added | duration_secs |
|---|---|---|
| `<sha8-C>` (= 別 export #1) | ~34k | 31.7 |
| `<sha8-D>` (= 別 export #2) | ~2.4k | 29.9 |
| `<sha8-B>` (= 上述 B3 の ZIP) | 1 | 29.6 |

**所見:** 増分 import でも wall-clock は約 30 秒で安定する。 短くなるのは
新規分の `records_added` のみで、 処理時間は 64MB 全体の展開 + XML パース
に律速される (= 増分 dedup は記録追加量を減らすが、 パース時間は減らさない)。

## E. MCPB bundle 経路 (Claude Desktop)

| 項目 | 結果 | 備考 |
|---|---|---|
| E1〜E4 | 実質達成 | 本 dogfood セッション全体が Desktop の MCPB bundle 経由で実行されており、 `list_zips` → `import_zip` (= background worker) → `get_import_status` polling → 結果報告という流れを複数回完走。 worker thread が daemon として bundle 経路で立ち上がることを実証 |

## F. 失敗シナリオ (= 例外パス)

すべて sync error の typed envelope を返し、 サーバーはクラッシュせず
後続 tool も正常応答。

| 項目 | 入力 | reason | 結果 |
|---|---|---|---|
| F1 | `<sha8-E>` (= HTML を .zip リネーム) | `invalid_zip` | OK |
| F2 | `<sha8-F>` (= xlsx zip、 AH マーカーなし) | `not_apple_health_export` | OK |
| F3 | `deadbeef` (= 存在しない hex) | `id_not_found` | OK |
| F4 | `zzzz` (= 非 hex) | `invalid_id` | OK |
| F5 | `APPLE_HEALTH_EXPORT_ZIPS_DIR` 未設定 | `export_zips_dir_not_set` | OK (= 再起動なしで反映、 `list_zips` も `export_zips_dir: null`) |
| F6 | `get_import_status(job_id="ij_nope")` | `job_not_found` | OK |

## G. v0.4.1 → v0.5.0 マイグレーション

- 5→6 マイグレを意図的に不採用とする方針のため、 本項目は moot
- fresh-reset 経路での `import_jobs` 作成は確認済 (= fresh DB で
  `schema_version: 6`、 `import_jobs` テーブル存在)

## H. 集客 narrative の裏取り

方針 (= 2026-06-29 決定): "ms で返る" を売るのは narrative にならない
(= ユーザ体験的には agent polling で結局 30 秒待つ)。 むしろ **ローエンド
ハードでも完走する**を具体スペックで実証する方が刺さる。 → H1 を
「N100 で何秒か」 に差し替え。

### N100 CLI bench 実測 — 2026-06-29

| 軸 | 値 |
|---|---|
| マシン | Intel N100 + NVMe (= ローエンド mini PC) |
| 入力 | 64 MB zip / ~1.2 GB export.xml (uncompressed) |
| 経路 | CLI `apple-health-mcp-server import <zip>` (= async polling のオーバヘッド込みじゃない純 import time) |
| **wall-clock** (`time` 実測) | **2m 7.8s** (= 127.8s、 uvx 起動 + zip 展開込み) |
| **orchestrator report** | **120.0s** (= import 本体) |
| Phase 1 (XML parse) | ~83s |
| Phase 2 (ECG) | <1s |
| Phase 3 (GPX) | ~30s |
| Phase 4 (Finalize) | ~6s |
| ingest counts | ~2.7M records / 356 workouts / 7 ECG / ~340k route points / ~1.6M metadata entries |

### スペックレンジ pin (= 高速 + ローエンドの 2 点)

| マシン | 経路 | duration |
|---|---|---|
| High-end laptop (Intel 14 + NVMe) | MCP async (A1.3) | **46.79s** |
| Low-end mini PC (Intel N100 + NVMe) | CLI sync (本 bench) | **120.0s** |

= ハイエンド ⇄ ローエンド mini PC で **47s 〜 120s** のレンジ。 v0.4 同期版
だとローエンドで client timeout で詰んでた壁が v0.5 async で消えた。

### 採用 narrative コピー (= LP / Reddit / awesome-mcp 投稿時の軸)

> **Runs on whatever you have: Apple Silicon laptop, $300 N100 mini PC,
> anything in between.**

採否ロジック: 「ms で返る」 「環境問わず動く」 等の抽象表現より、 ローエンド
機 (= N100 mini PC) を具体スペックとして提示する方が読者の自分事化を
起こせる (= 2026-06-29 判定)。

### H1〜H3 確定

| 項目 | 状態 | 内容 |
|---|---|---|
| H1 ローエンド完走実証 | **確定** | N100 で ~1.2 GB の Apple Health export を 120s で完走 (= 上記 table) |
| H2 import 中の read 応答性 | 未取得 | 「import 中でも他 tool が応答」 を agent ループから staged で踏めない (= D / B2 と同根)。 narrative の軸を H1 (= ローエンド完走) に振ったので必須素材ではない |
| H3 async error 経路 | 確定 | 失敗 import は typed error envelope を返し、 サーバーをクラッシュさせない (= F 項目で実証) |

## defect / 改善提案

### defect (バグ): 1 件

- **`list_zips` の `hint` 文字列が v0.4 の同期版のまま** (= v0.5.1 #187
  で対応済)。 v0.5.0 時点では `"The import takes 1-2 minutes for a
  typical multi-GB export; Claude will wait synchronously."` を返すが、
  実装は async (= 即 `queued` 返却 → polling)。 この文言は agent を
  polling ではなく同期待ちへ誤誘導しうる

### 機能追加案: 1 件

- **古い DB を開いた際の typed 警告** (= v0.5.1 #188 で対応済)。 v0.5.0
  時点では v0.4 系の既存 DB (= `schema_version < 6` かつ `import_jobs`
  不在) に 0.5.0 を被せて `import_zip` を叩くと、 生の `Catalog Error:
  Table with name import_jobs does not exist!` が露出する。 起動時
  `ensure_schema` で「想定テーブル不在 / schema_version が古い」 を検知し、
  typed エラーを返すことで AI・利用者ともに次の行動が一意に定まる。
  5→6 マイグレを書く (= 捨てて作り直す思想に反する) のではなく、 エラー
  導線のみを改善する案

## 次アクション候補 (= 当時の判断、 v0.5.1 で消化済)

- defect (= `list_zips` hint 文字列) を `type:fix` で起票し、 v0.5.1
  hot-fix に含める → **v0.5.1 #187 で完了**
- 機能追加案 (= `schema_outdated` typed 警告) の採否を判断 → **v0.5.1
  #188 で完了**
- 集客フェーズ復帰前に、 H1 (= return の ms) を CLI で計測して 1 値 pin
  → **N100 bench (= 120s) で確定**
- B2 / D の並行系を厳密に検証する場合は、 agent ループ外の並列発火
  スクリプトを用意 → 持ち越し

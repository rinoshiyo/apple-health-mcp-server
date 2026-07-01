# v0.5.0 adversarial (= 意地悪) テスト結果ログ (1 回目)

正常系 dogfood (`docs/dogfood/v0-5-0-dogfood-results.md`) の後、 仕様の
隙間・異常系・セキュリティ境界を狙って実施した追加検証の記録。
個人情報・ローカル環境固有値はスクラブ済。

本書は **1 回目** の adversarial review。 主に SQL safety / 入力境界 /
job 状態マトリクスを攻めた。 ここで発掘した `parquet_scan` /
`parquet_metadata` / `sniff_csv` 等の denylist エイリアス漏れを起点に
**2 回目** (= `v0-5-0-adversarial-results-2.md`) で HTTPS egress まで
確認 → stop-ship issue #190 (= v0.5.1 で fix) に発展した。

- 実施日: 2026-06-29
- サーバーバージョン: `0.5.0` / Claude Desktop (= MCPB bundle)
- DB: fresh 起点に複数 export を import 済 (= records 約 ~2.7M 件)

## 総括

- **最重要 (= 要対応)**: `run_custom_query` の関数ブロックは denylist
  方式で、 `parquet_scan` / `parquet_metadata` が**漏れている**。 これらは
  DB 外の任意ホストファイルを開きに行けるため、 ローカルファイル読み取り
  のバイパス経路になる (= 2 回目の追試で HTTPS egress も確認、 stop-ship
  認定 → v0.5.1 #190 で fix 済)
- read-only ガード本体 (= DDL / DML / 複文 / COPY / ATTACH / PRAGMA、
  および主要な fs 読み取り関数) は堅牢。 書き込み・破壊系はすべて阻止
  され、 全テーブルは無傷
- 持病だった「行数 / サイズ超過時の無言 truncate」 は解消。 行数上限は
  `truncated: true` で正直に通知、 1MB 超は loud なエラーで弾く
- その他は軽微 (= doc と挙動の不一致、 無言 coerce) で実害なし

## 1. `run_custom_query` の truncate 挙動 (= 持病の再検証)

| テスト | 入力 | 結果 |
|---|---|---|
| 行数上限 | HeartRate ~415k 件を LIMIT なし | `row_count: 1000` / `truncated: true` / payload も 1000 行。 フラグは正直 |
| サイズ超過 (1 行巨大) | `string_agg` で 1 行に全 hash 連結 | `Tool result is too large. Maximum size is 1MB.` のハードエラー。 **無言切りせず loud に失敗** |

**所見:** 過去にあった「サイズ超過で無言切り、 保存 JSON も切り詰め後のみ」
という挙動は再現せず。 行数上限は通知付き、 サイズ超過はエラー。 改善
されている。

## 2. read-only ガード破り (= セキュリティ)

### 2-1. 阻止された経路 (= 正常)

| 入力 | 結果 |
|---|---|
| `SELECT ...; DROP TABLE records;` (= 複文) | `Only a single SQL statement is allowed (got 2)` |
| `COPY (SELECT ...) TO '<path>/leak.csv'` (= ファイル書き込み) | `Only SELECT / WITH queries are allowed (DDL, DML, ATTACH, COPY, INSTALL, LOAD, PRAGMA, etc. are rejected)` |
| `SELECT * FROM glob('<path>/*')` | `Function 'glob' is not allowed (reads host files or external resources)` |
| `read_csv` / `read_csv_auto` / `read_text` / `read_blob` / `read_parquet` / `read_json` / `read_json_auto` | いずれも `Function '...' is not allowed` |
| `WITH x AS (SELECT * FROM read_text(...)) SELECT * FROM x` (= CTE 内に隠す) | ブロック (= 関数チェックは CTE 内へも再帰的に効く) |

### 2-2. **バイパス発見 (= 要対応)**

| 入力 | 結果 | 評価 |
|---|---|---|
| `SELECT * FROM parquet_scan('<path>/x.parquet')` | `IO Error: No files found that match the pattern ...` | **ブロックされず fs 読みに到達** (= `Function not allowed` ではなく IO エラー = ファイルが存在すれば読めた) |
| `SELECT * FROM parquet_metadata('<path>/win.ini')` | `Invalid Input Error: No magic bytes found at end of file '<path>/win.ini'` | **対象ファイルを実際に開いて末尾を読んだ**。 任意ファイルの存在確認・内容読み取りが可能 |

**根本原因 (推定):** 関数ブロックが denylist (= 禁止関数名の列挙) 方式
のため、 `read_parquet` のエイリアスである `parquet_scan` や、 メタデータ
系の `parquet_metadata` といった「同等の機能を持つ別名」 が列挙から漏れて
いる。 `read_parquet` 自体は塞がれているのに別名が通る、 という典型的な
denylist の穴。

**影響:** `run_custom_query` 経由で DB 外の任意ホストファイルを読める。
単一利用者のローカルツールである限り実リスクは限定的だが、 OSS として
公開し、 LLM エージェントが外部入力 (= ヘルスデータ内の文字列、 ユーザ
指示等) からクエリを組み立てる構成では、 ファイル読み取り・情報窃取の
経路になりうる。 公開前のハードニング項目として優先度高め。

**推奨対応:** denylist を allowlist に反転する (= 許可するテーブル関数・
スカラ関数を明示列挙し、 それ以外は一律拒否)。 DuckDB は fs / ネット
ワークに触れる関数・エイリアスが多く、 列挙漏れを潰し続けるのは現実的
でない。

→ v0.5.1 #190 では **engine-level lockdown (= `SET enable_external_access
= false`)** で対応 (= allowlist 反転より深い altitude で根本対処)。
denylist にも `parquet_scan` / `parquet_metadata` / `parquet_schema` /
`sniff_csv` を defense-in-depth で追加済。

### 2-3. 軽微な情報漏洩

| 入力 | 結果 | 評価 |
|---|---|---|
| `SELECT ... FROM duckdb_settings()` | 通過。 `temp_directory` 等の内部設定を取得 | 低リスク (= path は `get_server_info` で既出、 `allow_unsigned_extensions: false` で拡張ロードは不可)。 ただし introspection 系は fs 読み取り関数のブロック網にかからない傾向 |

→ v0.5.1 #190 で **engine-level lockdown** は適用済だが、 `duckdb_settings()`
は in-DB introspection 関数で外部リソース取得ではないため engine 設定の
対象外、 denylist にも未追加で保留 (= v0.6 #216 で起票済)。

## 3. id バリデーション境界 (= import_zip)

| 入力 | 期待 | 結果 |
|---|---|---|
| `abc` (= 4 文字未満) | 拒否 | `invalid_id` |
| 65 文字 hex (= 64 超) | 拒否 | `invalid_id` |
| `<SHA8>` (= 大文字 hex) | 仕様上 lowercase | **受理** (= 小文字化して冪等ヒット) |
| ` <sha8>` (= 先頭空白) | verbatim 想定 | **受理** (= trim して冪等ヒット) |

**所見:** バリデータは trim + lowercase してから判定する寛容仕様。 実害は
ないが、 docstring の「must be 4-64 **lowercase** hex characters」 / 「use
the id field **verbatim**」 と挙動が食い違う。 doc を実態 (= 大小文字・
前後空白を許容) に合わせるか、 厳格化するかのどちらかで整合を取るのが
望ましい (= 軽微)。

→ v0.5.1 #191 で **doc を実態に合わせる** 方針で対応済。

## 4. ページング境界

| 入力 | 結果 | 評価 |
|---|---|---|
| `limit: 0` | `limit must be >= 1` | OK (拒否) |
| `limit: 999999` (= 上限 1000 超) | 1000 に clamp、 `next_offset: 1000` | OK |
| `offset: -5` (= 負) | 先頭ページを返す (= 0 扱い、 無言 coerce) | 害なし。 エラーにはしない |
| `offset: 999999999` (= total 超) | `items: []` / `next_offset: null` | OK |

## 5. 日付フィルタ

| 入力 | 結果 | 評価 |
|---|---|---|
| `start_date: "2020-01-01' OR '1'='1"` (= SQLi 試行) | `Conversion Error: invalid timestamp field format` | **SQL インジェクション不可**。 文字列連結ではなく timestamp へ cast / パラメータ化されている |
| `start_date > end_date` | `[]` (= 空) | OK (= エラーにせず空) |

## 6. job 状態マトリクス

| 入力 | 結果 | 評価 |
|---|---|---|
| done 済み job_id の再 status | 永続した `ok` envelope を再返却 | OK (= 終端状態が `import_jobs` に残り冪等 read 可能) |
| 正しい形式の偽 job_id (`ij_<future-ts>_<sha8>_<rand>`) | `job_not_found` | OK |
| 不正形式 job_id (`ij_nope`、 正常系で実施済) | `job_not_found` | OK |

## 7. 破壊耐性 (= 最終確認)

上記の DROP / DELETE / COPY / 複文などの試行後に整合確認:

- `records`: ~2.7M 件 (= import により増加、 欠損なし)
- `import_jobs`: 5 行
- テーブル数: 21

**いずれの書き込み・破壊系も成立せず、 スキーマ・データは無傷**。

## defect / 改善提案 (= 本セッション分)

### 要対応 (= セキュリティ): 1 件

- **`run_custom_query` の関数 denylist にエイリアス漏れ** (= `parquet_scan`
  / `parquet_metadata`)。 DB 外の任意ホストファイルを読める。 denylist を
  allowlist に反転して恒久対処することを推奨。 `type:fix`、 優先度高
  (特に公開前)
  → v0.5.1 #190 で engine-level lockdown + denylist 追加で fix 済

### 軽微: 2 件

- **id バリデーションと docstring の不一致**。 大文字 hex・前後空白を受理
  するが、 doc は lowercase / verbatim を要求。 doc か実装のどちらかへ
  寄せて整合
  → v0.5.1 #191 で doc 側を実態に合わせる方針で fix 済
- **introspection 関数の扱い**。 `duckdb_settings()` 等が通り内部 path
  を取得可能。 低リスクだが、 allowlist 化の際に併せて方針決めするとよい
  → v0.6 #216 で起票 (= 未対応、 engine-level lockdown 対象外、
  defense-in-depth として denylist 追加検討)

### 参考 (= 良好だった点)

- read-only ガード本体 (= DDL / DML / 複文 / COPY / ATTACH / PRAGMA、
  主要 fs 読み取り関数、 CTE 内再帰チェック) は堅牢
- 日付パラメータ経由の SQL インジェクションは cast により不可
- truncate の無言切り (= 持病) は解消済
- 破壊系試行後もデータ・スキーマ無傷

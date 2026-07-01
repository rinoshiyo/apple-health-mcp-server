# v0.5.0 adversarial (= 意地悪) テスト結果ログ (2 回目)

1 回目 (`docs/dogfood/v0-5-0-adversarial-results.md`) で `parquet_scan` /
`parquet_metadata` の denylist 漏れ (= 任意ホストファイル読み取り) が
発覚した後、 追試として HTTPS / S3 URL + `sniff_csv` / `parquet_schema`
等の近縁関数を狙った 2 回目の review。 ここで **HTTPS egress + 実データ
漏洩 (= `sniff_csv` がファイル内容を返却)** を確認、 stop-ship issue #190
として確定 → v0.5.1 で fix 済。

個人情報・ローカル環境固有値はスクラブ済。

- 実施日: 2026-06-29
- サーバーバージョン: `0.5.0` / Claude Desktop (= MCPB bundle)
- DB: fresh 起点に複数 export を import 済 (= records 約 ~2.7M 件)

## 総括

- **最重要 (= 出荷ブロッカー)**: `run_custom_query` の関数ブロックは
  denylist 方式で、 `parquet_scan` / `parquet_metadata` / `parquet_schema`
  / `sniff_csv` が**漏れている**。 これらは DB 外の任意ホストファイルを
  開けるうえ (= `sniff_csv` はファイルの中身を結果として返す = 実データ
  漏洩を確認)、 `parquet_scan` は **HTTPS 経由で外部 URL をフェッチ
  できる** (= 追試で公開リポジトリの parquet を取得・内容返却を確認)。
  これはローカルファイル読み取り + **ネットワーク egress / SSRF** の経路
  であり、 本プロジェクトの根幹である「全データはユーザのローカルに
  留まる・外部送信なし」 という設計哲学に正面から反する。 **公開前の
  必須修正** → v0.5.1 #190 で fix 済
- read-only ガード本体 (= DDL / DML / 複文 / COPY / ATTACH / PRAGMA、
  および主要な fs 読み取り関数) は堅牢
- 持病だった「行数 / サイズ超過時の無言 truncate」 は解消
- その他は軽微 (= doc と挙動の不一致、 無言 coerce) で実害なし

## 1. `run_custom_query` の truncate 挙動 (= 持病の再検証)

1 回目と同結果 (= 行数上限は `truncated: true` 通知、 1MB 超は loud
エラー、 無言切り解消)。

## 2. read-only ガード破り (= セキュリティ、 2 回目の深掘り)

### 2-1. 阻止された経路 (= 正常)

1 回目と同じく、 複文 / COPY / DDL / DML / `read_csv` 系 / `glob` /
quoted-path table references は全 reject。

### 2-2. **バイパス発見 (= 出荷ブロッカー)**

| 入力 | 結果 | 評価 |
|---|---|---|
| `SELECT * FROM parquet_scan('<path>/x.parquet')` | `IO Error: No files found that match the pattern ...` | **ブロックされず fs 読みに到達** (= `Function not allowed` ではなく IO エラー = ファイルが存在すれば読めた) |
| `SELECT * FROM parquet_metadata('<path>/win.ini')` | `Invalid Input Error: No magic bytes found at end of file '<path>/win.ini'` | 対象ファイルを実際に開いて末尾を読んだ |
| `SELECT * FROM parquet_schema('<path>/win.ini')` | 同上 (= ファイルを開きに行った) | parquet 系メタ関数が軒並み素通り |
| `SELECT * FROM sniff_csv('<path>/win.ini')` | **対象ファイルの中身を読み、 1 行目由来のカラム名 `"; for 16-bit app support"` を結果に返却** | **実データ漏洩を確認**。 エラー止まりでなく中身が出る |
| `SELECT * FROM parquet_scan('https://raw.githubusercontent.com/.../blob.parquet')` | **外部 URL をフェッチし parquet の中身 (= 3 行) を返却** | **ネットワーク egress が可能**。 httpfs が有効。 SSRF (= 内部ネットワーク / クラウドメタデータ endpoint への到達) および「外部送信なし」 原則の破綻 |

**確認済の漏れ関数:** `parquet_scan` / `parquet_metadata` / `parquet_schema`
/ `sniff_csv`。 `read_parquet` 本体は塞がれているのにエイリアス・近縁
関数が通る。 `parquet_scan` は `file://` 相当のローカルパスに加え
`https://` URL も受ける。

**閉じている経路 (参考):** replacement scan (= `SELECT * FROM
'<path>/file'` の引用パス table 参照) は `Quoted-path table references
are not allowed` で別途ブロック済。 `read_csv` / `read_csv_auto` /
`read_text` / `read_blob` / `read_parquet` / `read_json` /
`read_json_auto` / `glob` は denylist 済。

**根本原因:** 関数ブロックが denylist (= 禁止関数名の列挙) 方式のため、
`read_parquet` のエイリアス `parquet_scan`、 メタデータ系
`parquet_metadata` / `parquet_schema`、 CSV スニファ `sniff_csv` など
「同等機能の別名・近縁関数」 が列挙から漏れている。 典型的な denylist
の穴であり、 漏れを潰し続けるのは原理的に不毛。

**影響 (= 深刻度: 高):**
- ローカル: `run_custom_query` 経由で DB 外の任意ホストファイルを読める
  (= `sniff_csv` は内容を直接返す)
- ネットワーク: `parquet_scan('https://...')` で外部 URL をフェッチでき、
  SSRF・外部疎通が成立。 **本プロジェクトの「外部送信なし」 という中核
  の約束を破る**
- OSS 公開し、 LLM エージェントが外部入力 (= ヘルスデータ内文字列・
  ユーザ指示・プロンプトインジェクション) からクエリを組む構成では、
  ファイル窃取・内部疎通の現実的な経路になる

**推奨対応:** denylist を allowlist に反転する (= 許可するテーブル関数・
スカラ関数を明示列挙し、 それ以外は一律拒否)。 あわせて httpfs /
ネットワーク拡張を無効化し、 外部 URL 参照自体を遮断する (= 「外部送信
なし」 を実装レベルで担保)。 DuckDB は fs / ネットワークに触れる関数・
エイリアスが多く、 列挙漏れを潰し続ける運用は破綻する。

→ v0.5.1 #190 では **engine-level lockdown** (= `SET enable_external_access
= false`) で対応 (= allowlist 反転を採用せず、 engine 設定 1 つで fs /
httpfs / S3 / GCS / Azure / ATTACH / COPY / INSTALL / LOAD まで全包括
遮断)。 denylist にも漏れた alias を defense-in-depth で追加済。

### 2-3. 軽微な情報漏洩

| 入力 | 結果 | 評価 |
|---|---|---|
| `SELECT ... FROM duckdb_settings()` | 通過。 `temp_directory` 等の内部設定を取得 | 低リスク。 ただし introspection 系は fs 読み取り関数のブロック網にかからない傾向 |

→ v0.5.1 #190 でも engine-level lockdown 対象外、 v0.6 #216 で起票
(= denylist 追加候補)。

## 3. id バリデーション境界 (= import_zip)

1 回目と同結果 (= trim + lowercase 寛容仕様、 doc との不一致は軽微)
→ v0.5.1 #191 で doc 側を実態に合わせて fix 済。

## 4. ページング境界

1 回目と同結果。

## 5. 日付フィルタ

1 回目と同結果 (= SQL injection 不可、 start > end は空)。

## 6. job 状態マトリクス

1 回目と同結果 (= done 済 job_id 永続、 偽 / 不正形式 job_id は
`job_not_found`)。

## 7. 破壊耐性 (= 最終確認)

上記の DROP / DELETE / COPY / 複文などの試行後に整合確認:

- `records`: ~2.7M 件 (= import により増加、 欠損なし)
- `import_jobs`: 5 行
- テーブル数: 21

**いずれの書き込み・破壊系も成立せず、 スキーマ・データは無傷**。

## defect / 改善提案 (= 本セッション分)

### 出荷ブロッカー (= セキュリティ): 1 件

- **`run_custom_query` の関数 denylist にエイリアス・近縁関数の漏れ**。
  `parquet_scan` / `parquet_metadata` / `parquet_schema` / `sniff_csv` が
  DB 外の任意ホストファイルを読め (= `sniff_csv` は内容を返却)、
  `parquet_scan` は HTTPS で外部 URL をフェッチできる (= ネットワーク
  egress / SSRF、 「外部送信なし」 原則の破綻)。 **対処: denylist →
  allowlist 反転 + httpfs / ネットワーク拡張の無効化**。 `type:fix`、
  最優先・公開前必須
  → v0.5.1 #190 で **engine-level lockdown** + denylist defense-in-depth
  で fix 済

### 軽微: 2 件

- **id バリデーションと docstring の不一致** → v0.5.1 #191 で fix 済
- **introspection 関数の扱い** (= `duckdb_settings()` 等が通り内部 path
  を取得可能) → v0.6 #216 で起票

### 参考 (= 良好だった点)

- read-only ガード本体 (= DDL / DML / 複文 / COPY / ATTACH / PRAGMA、
  主要 fs 読み取り関数、 CTE 内再帰チェック) は堅牢
- 日付パラメータ経由の SQL インジェクションは cast により不可
- truncate の無言切り (= 持病) は解消済
- 破壊系試行後もデータ・スキーマ無傷

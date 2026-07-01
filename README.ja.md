<!-- Read this in [English](README.md) / [日本語](README.ja.md) -->

# apple-health-mcp-server

[![PyPI version](https://img.shields.io/pypi/v/apple-health-mcp-server.svg)](https://pypi.org/project/apple-health-mcp-server/)
[![Python versions](https://img.shields.io/pypi/pyversions/apple-health-mcp-server.svg)](https://pypi.org/project/apple-health-mcp-server/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
[![Landing page](https://img.shields.io/badge/landing_page-rinoshiyo.github.io-10b981)](https://rinoshiyo.github.io/apple-health-mcp-server/)

> **Claude を、自分専属のヘルス AI に。**
>
> Read this in [English](README.md) / [日本語](README.ja.md).

`apple-health-mcp-server` は、Apple Health の書き出し（`export.xml`
および同梱の ECG CSV / GPX ルートファイル群）を、ローカル
[DuckDB](https://duckdb.org/) データベースを介して任意の
[Model Context Protocol](https://modelcontextprotocol.io/) クライアント
（Claude Desktop を含む）へ公開します。MCP ツールは合計 21 個（読み取り 18 + ZIP フロー 2 + ジョブ状態 1）で構成されます。

## 特徴

- **網羅的な取り込み。** `Record`, `Workout`（`WorkoutEvent` /
  `WorkoutStatistics` / `WorkoutRoute` / `WorkoutMetadataEntry` を含む）、
  `ActivitySummary`, `Correlation`, `Me`, `ExportDate`、ECG 電圧サンプル、
  GPX ルート点を取り込みます。iOS 17 以降のカテゴリ型「気分」エントリは
  専用テーブルに格納されます。
- **すべてローカル完結 — 外部送信なし。** インポーターはディスクから
  ファイルを読み、サーバーは MCP を stdio で話します（HTTP は opt-in）。
  ネットワークに何かが流れるとすれば、それはクライアント側の判断によるものです。
- **DuckDB バックエンド。** 決定的な重複排除により、同じデータの再インポートは
  冪等です。`run_custom_query` 経由のアドホック分析は DuckDB のネイティブ速度で動きます。
- **タイムゾーン整合。** GPX ルートのタイムスタンプは各親ワークアウトの
  ローカルオフセットに合わせるため、XML 由来の行とクリーンに結合できます。
- **クロスプラットフォーム。** Ubuntu / macOS / Windows × Python 3.12 / 3.13 /
  3.14 でテスト済み。
- **分岐網羅 100%。** すべてのリリースは
  `pytest --cov-branch --cov-fail-under=100` をゲートします。

## インストール

推奨は [uvx](https://docs.astral.sh/uv/) です。必要なときだけ一回限りの
仮想環境を取得し、システム Python を汚しません。

```bash
uvx apple-health-mcp-server --help
```

### Claude Desktop（MCPB バンドルでワンクリック）

Claude Desktop で最も簡単な手順は、 各
[GitHub Release](https://github.com/rinoshiyo/apple-health-mcp-server/releases)
に添付された **MCPB バンドル** を使う方法です。

> **前提条件:** バンドルは `uvx apple-health-mcp-server serve` を
> ラップしているため、 先に [`uv`](https://docs.astral.sh/uv/) を
> インストールしてください（macOS は `brew install uv`、 Windows は
> 公式インストーラ）。 `PATH` に `uv` が無いと Claude Desktop は
> インストール後に汎用的な spawn エラーで失敗します。

> **初回起動の warmup (推奨)。** バンドル install 前にターミナルで
> **1 回だけ** 下記を実行してください:
>
> ```bash
> uvx --from "apple-health-mcp-server@latest" apple-health-mcp-server --help
> ```
>
> Claude Desktop は初回起動で MCP サーバを並列に複数 spawn します。
> 各 `uvx` 呼び出しが同じ Python interpreter を同時にインストール
> しようとして minor-version-link directory で競合し、 cache が
> 半端な状態で残り、 以降の起動が毎回 `Missing expected target
> directory for Python minor version link` で失敗します。 事前に
> 1 回 warmup しておけば install が直列化されて回避できます。
> 顕在化が確認されているのは Windows ですが、 macOS / Linux でも
> 理論上同じ race が起こりうる (file system 的にはより安全寄り) ため、
> 全 OS で warmup を 1 回挟むのが安価で確実な予防策です。

その後:

1. [最新リリースページ](https://github.com/rinoshiyo/apple-health-mcp-server/releases/latest)
   から最新の `apple-health-mcp-server-vX.Y.Z.mcpb` バンドルを
   ダウンロード（リンクは現行 `vX.Y.Z` に自動解決されるため、
   新バージョンが出ても URL は腐りません）
2. Claude Desktop の **Settings → Connectors** パネルを開く
3. `.mcpb` ファイルをパネルにドラッグ&ドロップ — Claude Desktop が
   インストールしてサーバ有効化を確認するプロンプトを出します
4. install ダイアログで **Export ZIPs directory** を指定します。
   Apple Health 書き出し ZIP を置くフォルダ (Windows なら
   `C:\Users\<you>\Documents\AppleHealth`、 macOS / Linux なら
   `~/Documents/AppleHealth` 等) を選び、 そこに `export.zip` を
   置いて Claude に「Apple Health export をインポートして」 と頼む
   だけで Claude が `list_zips` → `import_zip(id="…")` を呼びます。
   `import_zip` は `job_id` を即座に返してバックグラウンドワーカーで
   インポートを実行し、 Claude は `get_import_status(job_id=…)` を
   10〜30 秒おきに `ok` (または `error`) になるまで poll します。
   大きな export では取り込みに数分かかることがあります。 ターミナル
   操作は不要です。
5. **Windows ユーザのみ — `%LOCALAPPDATA%` 配下は避けてください。**
   Windows 版 Claude Desktop は MSIX パッケージで、 子プロセスは
   AppContainer サンドボックス内で動き `%LOCALAPPDATA%` が
   per-package 専用パスに virtualise されます。 Export ZIPs
   directory がそこ配下だと、 Explorer から ZIP を置いても
   `list_zips` に見えません。 `%USERPROFILE%` 配下
   (Documents、 Desktop 等) を選んでください。

MCPB フォーマット仕様は <https://github.com/anthropics/mcpb> を参照。
`.dxt` (旧名) と `.mcpb` どちらの拡張子も Claude Desktop が受け付けます。

### Claude Desktop（手動 JSON 設定）

手動で配線したい場合は `claude_desktop_config.json` を編集します。

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`
- Linux: Claude Desktop の Linux 版はまだ提供されていません。下記の
  **Claude Code** を利用してください。

```json
{
  "mcpServers": {
    "apple-health": {
      "command": "uvx",
      "args": ["apple-health-mcp-server", "serve"],
      "env": {
        "APPLE_HEALTH_EXPORT_ZIPS_DIR": "/Users/<you>/Documents/AppleHealth"
      }
    }
  }
}
```

`APPLE_HEALTH_EXPORT_ZIPS_DIR` は Apple Health 書き出し ZIP を
置くフォルダです。 v0.4 の `list_zips` / `import_zip` MCP ツール
がこのディレクトリを読みに行くので、 設定しておくと Claude が
ターミナルを開かずに ZIP を発見 + 取り込みできます。 上記のパス
は実フォルダに置き換えてください (例: macOS / Linux なら
`~/Documents/AppleHealth` をシェルで展開した絶対パス、 Windows
なら `C:\Users\<you>\Documents\AppleHealth`)。 MCPB バンドル
install ダイアログの **Export ZIPs directory** フィールドはこの
env 変数に注入されるので、 JSON も MCPB も最終的に同じ環境変数
を server に渡します。

設定ファイルは起動時にだけ読み込まれるため、Claude Desktop を**完全に
終了**してから再度開いてください（ウィンドウを閉じるだけでは反映されま
せん）。

出典: <https://modelcontextprotocol.io/quickstart/user> （取得 2026-06-22）

### Claude Code

公式 CLI で追加するのが最も確実です。スコープを指定したうえで正しい設定
ファイルに書き込まれます。

```bash
claude mcp add --transport stdio --scope user \
  --env APPLE_HEALTH_EXPORT_ZIPS_DIR=$HOME/Documents/AppleHealth \
  apple-health -- uvx apple-health-mcp-server serve
```

- `--scope user` は全プロジェクト共通の登録です（`~/.claude.json` に書
  き込まれます）。プロジェクト内でチーム共有したいときは `--scope project`
  を指定して `.mcp.json` をリポジトリにコミット、現在のプロジェクトだけ
  で使いたい場合は既定の `--scope local` を選択してください。
- `--env APPLE_HEALTH_EXPORT_ZIPS_DIR=…` は v0.4 の ZIP フロー
  ツール (`list_zips` / `import_zip`) が読みに行く Apple Health
  書き出し ZIP の置き場所を指定します。 未設定だとこれらのツール
  は `NEEDS_CONFIG` エンベロープを返し、 エージェントが取り込み
  前にあなたへ設定を促す流れになります。
- サーバーコマンドに独自の引数を渡すときは `--` セパレータが必須です。
  `--` がないと `serve` が Claude Code 側のオプションとして解釈されます。

手動で JSON を書く場合は次のようになります。

```json
{
  "mcpServers": {
    "apple-health": {
      "type": "stdio",
      "command": "uvx",
      "args": ["apple-health-mcp-server", "serve"],
      "env": {
        "APPLE_HEALTH_EXPORT_ZIPS_DIR": "/Users/<you>/Documents/AppleHealth"
      }
    }
  }
}
```

セッション中に `.mcp.json` を編集してもホットリロードはされません。
Claude Code を再起動して反映させてください。stdio サーバーがクラッシュ
した場合も自動再接続はされないため、サーバーが落ちたら再起動が必要で
す。

出典: <https://code.claude.com/docs/en/mcp> （取得 2026-06-22）

### Codex CLI

Codex CLI は設定を **TOML** で保存します（他クライアントは JSON）。最も
簡単なのは CLI ヘルパで `~/.codex/config.toml` に書き込む方法です。

```bash
codex mcp add apple-health -- uvx apple-health-mcp-server serve
```

`~/.codex/config.toml` に直接書く場合（`CODEX_HOME=` 環境変数でパス変
更可）:

```toml
[mcp_servers.apple-health]
command = "uvx"
args = ["apple-health-mcp-server", "serve"]
env = { APPLE_HEALTH_EXPORT_ZIPS_DIR = "/Users/<you>/Documents/AppleHealth" }
```

`APPLE_HEALTH_EXPORT_ZIPS_DIR` は v0.4 の ZIP フローツール
(`list_zips` / `import_zip`) が読みに行く Apple Health 書き出し
ZIP の置き場所です。 未設定だとこれらのツールは `NEEDS_CONFIG`
エンベロープを返し、 エージェントが取り込み前にあなたへ設定を
促す流れになります。

`config.toml` の編集内容は次回 `codex` 起動時に反映されます。実行中の
セッションを再起動してください。`codex mcp list` / `codex mcp get <name>`
/ `codex mcp remove <name>` で確認・削除も行えます。

出典: <https://developers.openai.com/codex/mcp> （取得 2026-06-22）

### データのインポート

ツールから意味のある結果を得るには、最初に一度 Apple Health のエクス
ポートを取り込みます。Apple ヘルスケアの「共有 → 書き出し」 で生成さ
れる `export.zip` をそのまま CLI に渡してください。 **v0.5 以降、CLI
は ZIP パスを直接受け取ります — 手元で解凍する必要はなくなりました**。

```bash
uvx apple-health-mcp-server import /path/to/export.zip
```

CLI は内部で ZIP を一時ディレクトリに展開し、MCP の `import_zip`
ツールと同じパイプラインを通します。CLI 経由でも MCP 経由でも
同じ `imports.source_zip_*` 値が記録されるので、冪等性は CLI/MCP
の境界を跨いで効きます。 完全に同じ ZIP で再実行するとミリ秒で
no-op になり、 新しいエクスポートで再実行すると `import_id`
列により既存データベースに追記される形で統合されます。

Phase 1（XML パース）では 10 秒ごとに進捗ログを 1 行
出力します（`INFO progress: xml NN% (X / Y MiB, ~Z min remaining)` —
スループットは 1 MiB / 1,048,576 バイトの読み出しチャンクに合わせて
いるため "MiB" 表記）。ストリーミングで監視している AI エージェントや
人間が、数分におよぶパース中も処理が進んでいることを確認できます。
間隔は環境変数 `APPLE_HEALTH_IMPORT_PROGRESS_SECS`（正の整数、1..600
にクランプ）で変更でき、静かに走らせたいときは `60`、デバッグ時は `1`
などに設定します。1 MB（1,000,000 バイト — 10 進）未満の
エクスポートでは進捗行を出力しません。

<a id="database-location-ja"></a>
### データベースの場所

既定では XDG 準拠のデータディレクトリに格納されます。

- Linux / macOS: `~/.local/share/apple-health-mcp/health.duckdb`
- Windows: `%LOCALAPPDATA%\apple-health-mcp\health.duckdb`

上書きの優先順位 (高い → 低い):

1. `--db /custom/path/health.duckdb` (両サブコマンド共通)。
2. `APPLE_HEALTH_DB` 環境変数 (ファイルパス) — `--db` と同じ優先度
   (CLI 側で `--db` を APPLE_HEALTH_DB に promote するため、 同じ
   パスを resolver が見るようにしてあります)。 v0.4 から MCPB
   バンドルの GUI フィールドは削除されたので、 Claude Desktop で
   カスタム DB パスを使いたい場合は `claude_desktop_config.json`
   を直接編集し、 サーバの `env` マップに `APPLE_HEALTH_DB` を追加
   してください (上記の手動 JSON 設定セクション参照)。
   しているため、 `resolve_db_path()` を経由する全 caller (将来の
   subcommand、 plugin、 `get_server_info` 等) が同一パスに合意できます。
3. `APPLE_HEALTH_DATA_DIR` 環境変数 (ディレクトリパス) — root だけ
   カスタマイズしてパッケージのデフォルト file 名を維持したい時に。
4. 上記の XDG / `LOCALAPPDATA` プラットフォーム既定。

`get_server_info` MCP tool で稼働中のサーバが実際に開いている
パスと、 どの override 段階が解決したかをいつでも確認できます。
Windows MSIX サンドボックスリダイレクト (Claude Desktop install
セクション参照) のトラブルシュートで便利です。

### ロケール

Apple Health は ECG CSV のヘッダラベルを iPhone の言語設定に応じてローカライズします
（`export.xml` 自体はロケール非依存です）。 importer がサポートするのは:

- **検証済**: 英語、 日本語 (`記録日` と `記録日時` の両表記)
- **ベストエフォート**: 簡体中文、 繁體中文、 韓国語 — 実際のローカライズ済みエクスポートで
  未検証で、 ラベル文字列は推定値です

サポート対象ロケールの正本は `src/apple_health_mcp/importers/ecg.py` の
`_VERIFIED_LOCALES` / `_BEST_EFFORT_LOCALES` タプル（および対応する
`_*_LABELS` タプル群）です。 新規ロケール追加時は当該ファイルとそれらの
タプルを更新し、 本 README はそれを反映する形で読んでください。

> **異ロケール export のマージは v0.3.x では未対応 (issue #131)。**
> Apple Health は CSV ヘッダだけでなく **フィールド値そのもの** も
> iPhone 表示言語に応じてローカライズします: 日本語ロケール export
> では ECG `classification` が `洞調律`、 英語 export では
> `SinusRhythm`、 `source_name` は `ヘルスケア` / `血中酸素ウェルネス`
> 対 `Health` / `Blood Oxygen` となります。 importer は値をそのまま
> 格納し、 名寄せ正規化レイヤは無いため、 **異なる** iPhone ロケールの
> export を **同じ** DB にマージすると下流 tool で reconcile できない
> 並行行集合ができます。 正規化が入るまで 1 ロケール 1 DB 運用を
> 推奨します (必要な方は #131 に thumbs-up お願いします)。

どのロケールにもマッチしなかった場合、 警告ログが GitHub Issue Tracker への報告を促します
（CSV の最初の 10 行を添付してください）。 完全なガイダンスは 1 回の import 実行で 1 度だけ
emit され、 同じ実行内の以降のファイルは短い参照行のみになります。 ヘッダ部分には個人情報は
含まれません（`Name` と `Date of Birth` は importer が意図的にスキップします）。

距離・エネルギーの単位 (`km`, `mi`, `kcal`) は HealthKit 識別子由来で、 ローカライズされません。
`workouts` テーブルの `total_distance_unit` カラムに正確に記録されます。

## ツール群

FastMCP に登録される 21 ツールを系統別にまとめます。

| 系統 | ツール |
|---|---|
| レコード種別とデータ | `list_record_types`, `query_records`, `get_record_statistics` |
| ワークアウト | `list_workouts`, `get_workout_details`, `get_workout_route` |
| アクティビティサマリー | `get_activity_summaries` |
| 心拍 | `get_heart_rate_samples` |
| 相関 | `list_correlations`, `get_correlation_details` |
| ECG | `list_ecg_readings`, `get_ecg_data` |
| 気分（State of Mind） | `list_state_of_mind` |
| Me 属性 | `get_me_attributes` |
| メタデータ・運用 | `list_data_sources`, `get_import_history` |
| エスケープハッチ | `run_custom_query`（読み取り専用の検証済み SQL） |

## 互換性

`apple-health-mcp-server` は v1.0.0 以降
[Semantic Versioning](https://semver.org/lang/ja/) に従います。 v0.x.y
系列の間はマイナーリリースでも破壊的変更が含まれる可能性があり、
極力避ける方針ではあるものの formal な保証はまだありません。

### 二層構造の契約

public surface は二層構造に分け、 内部ストレージの選択を改善しても
wire-facing な契約が毎度メジャーバンプを要求しないように整理して
います。

**Layer 1 — wire-facing 契約 (strict、 変更はメジャーバンプ):**

- **MCP ツール名、 パラメータシグネチャ (デフォルト値含む)、
  トップレベルのレスポンスフィールド名** — 新ツール / 新パラメータ /
  新レスポンスフィールド追加はマイナーバンプ、 既存項目のリネーム /
  削除 / 型変更はメジャーバンプ。 ツールのレスポンスは下流の LLM
  プロンプトテンプレートが消費するため、 返却キーのリネームは
  パラメータのリネームと同等の破壊変更
- **CLI サブコマンド名と必須パラメータ** (positional 引数と必須
  フラグの両方)、 および **環境変数名とパース仕様** — リネーム /
  削除 / セマンティクス変更はメジャーバンプ
- **CLI 終了コード** (詳細は下記の表)
- **パッケージルート (`apple_health_mcp`) から `__all__` で
  エクスポートされるトップレベルの Python 識別子** — 例
  `__version__`, `REPO_URL`, `ISSUES_URL`。 削除や型変更はメジャー
  バンプ

**Layer 2 — internal escape hatch (best-effort、 変更はマイナー
バンプ + CHANGELOG.md `Changed` で明示):**

- **DuckDB スキーマ** — テーブル名、 カラム名、 型、 NOT NULL 制約。
  `run_custom_query` でテーブルへ SQL を直接書く利用者がここに依存
  するため軽率には変更しないが、 スキーマは wire 契約ではなく
  ストレージの実装詳細であって、 カラムリネームや型拡張は
  CHANGELOG.md の `Changed` で明示することを条件にマイナーリリース
  でも入り得る。 Layer 1 のツールレスポンスはその上に組み立てられて
  いるため、 ツール出力に影響しないスキーマ移行は
  `run_custom_query` 以外の呼び出し側からは不可視のまま済む
- **デフォルト DuckDB ファイルパス規約** (詳細は
  [データベースの場所](#データベースの場所)) — 各 OS の XDG 準拠
  デフォルトパスは実運用上は安定 (ユーザはバックアップ対象にしたり
  監視を向けたり symlink を貼ったりする) だが、 Layer 2 に置く
  ことで、 追加のオーバーライド機構を導入したり、 OS の規約変更に
  応じてデフォルトを調整する余地をメジャーバンプなしに確保する
- **モジュール内部のヘルパー** — `apple_health_mcp.__all__` から
  re-export されていないもの。 コントリビュータ向けに inline で
  ドキュメントしているが、 どの tier でも SemVer 契約の対象外

`run_custom_query` を使う利用者は構造上 Layer 2 に依存する。 安定性は
best-effort 扱い — マイナーバージョン間ではできる限りスキーマ変更を
避け、 変更が入った場合は CHANGELOG.md の `Changed` で明示するため、
既存のカスタムクエリは 1 パスで更新できる。

スキーマ移行は forward-only。 スキーマバンプ後に旧バージョンへ
ダウングレード (例: v0.3.0-rc2 → v0.2.x) する場合は、 `export.xml`
から再 import するか、 スキーマバンプ前の DB バックアップを復元する
必要がある。

#### Layer 1 リファレンステーブル

**サーバ / importer がプロセス環境から読む環境変数** — 現状のセット:

| 名前 | 用途 | デフォルト |
|---|---|---|
| `APPLE_HEALTH_TZ` | DuckDB セッションタイムゾーン。 `TIMESTAMPTZ` カラムのレンダリングに使用。 CLI の `--tz` 指定時はそちらが優先される | OS の TZ |
| `APPLE_HEALTH_IMPORT_PROGRESS_SECS` | `import` の Phase 1 進捗 emitter の間隔。 整数秒、 範囲外の整数は 1..600 にクランプ、 非整数文字列は警告ログを出してデフォルトにフォールバック。 1 MB（1,000,000 バイト — 10 進）未満のエクスポートは emitter 自体をスキップ。 デフォルト値・クランプ範囲は `apple_health_mcp.importers.xml._PROGRESS_INTERVAL_{DEFAULT,MIN,MAX}_SECS` 由来で、 README が定数からずれたら `tests/unit/test_docs_in_sync.py` の doc-test が CI で失敗する | `10` |
| `APPLE_HEALTH_LOG_LEVEL` | stdlib `logging` のルートロガーレベル (`DEBUG`/`INFO`/`WARNING`/`ERROR`)。 全ログは stderr 行き、 stdout は MCP stdio transport が占有 | `INFO` |
| `APPLE_HEALTH_LOG_FORMAT` | ログフォーマッタ形式。 `human` はプレーンテキスト、 `json` は 1 行 1 オブジェクトの JSON でログアグリゲータ向け | `human` |

サーバは DB デフォルトパス解決時に OS 標準の `XDG_DATA_HOME` (Linux/macOS) と `LOCALAPPDATA` (Windows) も honour する — デフォルトパス文字列の正本は [データベースの場所](#database-location-ja) を参照。 これら OS 変数はプラットフォーム契約であってプロジェクト固有変数ではない。

これらのリネーム / 削除 / パース仕様変更はメジャーバンプ。 新規 env var 追加はマイナーバンプ。

**CLI パラメータ** — `apple-health-mcp-server` をシェルスクリプト / サービススーパーバイザに食わせる、 あるいは Claude Desktop / Claude Code config に組み込む呼び出し側向けの契約:

- **サブコマンド**: `import <export.zip>` （v0.5 以降。 pre-v0.5 はディレクトリ受付）、`serve`
- **トップレベルフラグ**: `--db <path>` (DB パス上書き、 両サブコマンドで有効)、 `--tz <name>` (`APPLE_HEALTH_TZ` を上書き)
- **`serve` フラグ**: `--transport stdio|http` (デフォルト `stdio`)、 `--host <addr>` (HTTP バインドホスト)、 `--port <int>` (HTTP ポート)

サブコマンドやフラグのリネーム / 削除 / 既存項目のセマンティクス変更はメジャーバンプ。 新規 optional フラグやサブコマンドの追加はマイナーバンプ。

**CLI 終了コード** — シェルスクリプト呼び出し側が観測する:

| コード | 意味 |
|---|---|
| `0` | 成功 |
| `1` | import / serve パス内の任意の `AppleHealthMCPError` (エクスポート不在、 DB 破損、 importer 失敗、 サーバ起動失敗) |
| `2` | CLI 引数パーサ層の usage error (未知サブコマンド、 必須引数欠落、 不正フラグ値) |

新規の specific exit code 追加 (例: 「他プロセスが DB ロック中」 を `3` に切り出す等) はマイナーバンプ。 既存コードの **意味の付け替え (repurpose) や統合** はメジャーバンプ。

#### どちらの層にも含まれないもの

Layer 1 / Layer 2 のいずれにも列挙されていないもの — MCP ツール /
CLI / `__all__` / env var / exit code の表面を持たないヘルパー
モジュール、 `_` プレフィックス付き識別子（private 定数、 ヘルパー、
internal 例外）、 モジュール内部の定数 — はどの tier でも
**public API ではなく**、 任意のリリースで変更されます。 特に:

- **ログ行のフォーマット** (例: `progress: xml NN% (X / Y MiB, ~Z min remaining)`)
  は public API 契約の一部ではありません。 人間向けの表記は SemVer
  バンプなしに変更され得ます。 `APPLE_HEALTH_LOG_FORMAT=json` は現状、 同じ人間
  向け文字列を JSON envelope の `message` フィールドに包むだけで、
  progress 専用の構造化フィールドはまだ emit していません。 機械的に
  パースしたい用途があれば issue を立ててください — 構造化 progress
  契約が公開されるまでは、 progress 出力は informational のみと扱って
  ください
- **MCP ツールの description テキスト** (各ツール登録時の LLM 向け
  プロンプト文面) は public API 契約の一部ではありません。 パラメータ
  と戻り値の shape が変わらない限り、 description の文言は SemVer
  バンプなしに改善・整理されます。 クライアントはツールの **名前と
  シグネチャ** を契約として参照し、 description prose には依存しない
  でください

### 非推奨ポリシー

(v1.0.0 以降に適用 — v0.x.y の間はこの cadence を経由せずに
マイナーリリースでも破壊的変更が入り得ます。 詳細はヘッドラインを
参照)

public API から何かを削除 / リネームする際:

1. その変更を announce するリリースの CHANGELOG.md `Deprecated`
   セクションに、 代替案と削除予定バージョンとセットで記載
2. 非推奨化した項目は **少なくとも 1 マイナーリリース**は動作を維持
   （例: `1.5.0` で非推奨化 announce、 `1.6.x` 系は旧名のまま出荷、
   `2.0.0` で削除）
3. 実際の削除は次のメジャーバージョンバンプで実施

### セキュリティ例外

public API の非推奨化された surface 内に CVE 級の脆弱性が見つかった場合
（例: `run_custom_query` のパラメータがデータ漏洩経路になっていた、
ツールのレスポンス形状が見せてはいけない情報を露出していた、 等）、
上記の deprecation cadence を破ってもよい — patch を含む **任意の
リリース**で削除や破壊的変更を伴う修正を出荷できます。 該当する破壊
変更は CHANGELOG.md の `Security` 見出しに記載し、 GitHub リポジトリの
Security タブにセキュリティアドバイザリを公開します。 この carve-out
がない場合、 deprecation policy に縛られて known-bad な surface を
1 マイナーサイクル維持することになり、 突然の破壊変更よりも悪い結果を
招くため、 明示的に例外として規定しています。

## アップデート

`uvx` は初回実行時にパッケージをキャッシュし、 以降はそのキャッシュを再利用
するため、 新しいリリースを公開しても**自動では更新されません**。 用途に応
じて以下のいずれかを選んでください。

- **常に最新版を取得する** — 最新の公開バージョンを使いたい場合は
  `@latest` サフィックスを付けて実行します。

  ```bash
  uvx apple-health-mcp-server@latest serve
  ```

  > **なぜ `--refresh` ではないのか？** `--refresh` は PyPI のメタデータを
  > 再検証するものの、 キャッシュ済みのツール環境を必ず再構築するわけでは
  > なく、 新しいリリースが公開されていても以前のキャッシュが黙って使われ
  > 続けることがあります（[astral-sh/uv#16991](https://github.com/astral-sh/uv/pull/16991)）。
  > `@latest` は [uv 公式ドキュメント](https://docs.astral.sh/uv/concepts/tools/)
  > が推奨している曖昧さのない方法です。

- **特定バージョンに固定する** — Claude Desktop / Codex / Cursor の設定で
  バージョンを明記しておくと、 `uvx` のキャッシュが消えても固定版が維持さ
  れます。

  ```jsonc
  {
    "mcpServers": {
      "apple-health": {
        "command": "uvx",
        "args": ["apple-health-mcp-server==0.1.0", "serve"]
      }
    }
  }
  ```

リリースごとの変更点は [CHANGELOG.md](./CHANGELOG.md) を参照してください。

### v0.3.0 未満からのアップグレード

v0.3.0 では、 v0.3.0 未満の DB を対象とした自動スキーマ移行を廃止しまし
た（[issue #124](https://github.com/rinoshiyo/apple-health-mcp-server/issues/124)
参照）。 古い DB に対して `apple-health-mcp-server serve` を最初に実行する
と、 パスと復旧コマンドを含む `ConfigError` を出して終了します。 ディス
ク上のデータには一切手を加えません。

復旧は一度きりの再インポートで完了します。 下記スニペットはデフォルト
DB パス（[データベースの場所](#database-location-ja) 参照）を前提と
しています。 カスタムパスを使っている場合は `--db /custom/path/health.duckdb`
を添えてください。

```bash
# v0.3.0 未満の DB を削除。
rm ~/.local/share/apple-health-mcp/health.duckdb

# 直近の Apple ヘルスケア export.zip を CLI に直接渡して再インポート
# （v0.5 以降は ZIP のまま渡せます。 pre-v0.5 は手元で解凍が必要でした）。
uvx apple-health-mcp-server@latest import /path/to/export.zip
```

数 GB の `export.xml` でもインポートは数分で完了し、 データがマシン外に
出ることはありません。 再インポート後、 以降の `serve` 実行は v0.3.0 ス
キーマに対して動作し、 ConfigError は二度と出ません。

## トラブルシューティング

**どのツールも `{"state": "NEEDS_CONFIG" | "NEEDS_IMPORT", ...}` 形式の構造化エンベロープを返す**

ローカルの DuckDB ファイルが空でも MCP サーバーは起動するように
なっており（クライアントから全ツールが見えるようにするため）、
読み取り系ツールはインポートが完了するまで以下のような構造化
JSON エンベロープを返します:

```json
{
  "state": "NEEDS_CONFIG",
  "reason": "env_unset",
  "suggested_action": "ask_user_to_open_settings",
  "human_message": "Set the APPLE_HEALTH_EXPORT_ZIPS_DIR ..."
}
```

`state` は次のいずれかです:

- `NEEDS_CONFIG` — `APPLE_HEALTH_EXPORT_ZIPS_DIR` 環境変数
  （v0.4 の `list_zips` / `import_zip` MCP ツールが読みに行く ZIP の
  置き場所）が未設定。 Claude Desktop ユーザーは Settings → MCP →
  apple-health-mcp-server → Export ZIPs directory から、 それ以外の
  MCP クライアントは環境変数を直接設定します。
- `NEEDS_IMPORT` — 置き場所は設定済みだがまだインポート成功行が
  ない。 Claude に `list_zips` → `import_zip(id="…")` の順で呼ばせて
  ください。

CLI 経由のインポートは:

```bash
apple-health-mcp-server import /path/to/export.zip
```

**先に MCP サーバーを停止してから** CLI インポートを走らせてくだ
さい（Claude Desktop を終了する、 `serve` プロセスを kill する等）。
`serve` プロセスは `import_zip` ツールがバックグラウンドワーカーを
起動できるよう DuckDB ハンドルを書き込み可能で開いており、 DuckDB は
書き込みハンドルが生きている間ファイルに対する排他ロックを保持します。
別シェルから `apple-health-mcp-server import` を打つと lock 衝突
エラーになるため、 サーバーを止めてから実行し、 完了後に起動し
直すという順序が必要です。

`get_import_history` は空 DB でも呼び出せる唯一のツールで、
空配列を返します。 クライアント側から「まだインポートしていない」
状態を確認する手段として機能します。

## 開発

```bash
uv sync
uv run pytest
```

開発コマンドの完全な一覧、コーディング規約、PR ごとの `/code-review --fix`
必須運用は [CLAUDE.md](./CLAUDE.md) を参照してください。

## コントリビューション

Issues / Pull Requests は **英語と日本語のどちらも歓迎** します。
言語ポリシーの全容は [CLAUDE.md §6](./CLAUDE.md#6-language-policy)
を参照してください。

## ライセンス

[MIT](./LICENSE)

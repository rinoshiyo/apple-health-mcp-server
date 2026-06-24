<!-- Read this in [English](README.md) / [日本語](README.ja.md) -->

# apple-health-mcp-server

[![PyPI version](https://img.shields.io/pypi/v/apple-health-mcp-server.svg)](https://pypi.org/project/apple-health-mcp-server/)
[![Python versions](https://img.shields.io/pypi/pyversions/apple-health-mcp-server.svg)](https://pypi.org/project/apple-health-mcp-server/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)

> **おそらく、もっとも網羅的な Apple Health MCP サーバー。**
>
> Read this in [English](README.md) / [日本語](README.ja.md).

`apple-health-mcp-server` は、Apple Health の書き出し（`export.xml`
および同梱の ECG CSV / GPX ルートファイル群）を、ローカル
[DuckDB](https://duckdb.org/) データベースを介して任意の
[Model Context Protocol](https://modelcontextprotocol.io/) クライアント
（Claude Desktop を含む）へ公開します。読み取り中心の 17 ツールで構成されます。

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

### Claude Desktop

`claude_desktop_config.json` を編集します。

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`
- Linux: Claude Desktop の Linux 版はまだ提供されていません。下記の
  **Claude Code** を利用してください。

```json
{
  "mcpServers": {
    "apple-health": {
      "command": "uvx",
      "args": ["apple-health-mcp-server", "serve"]
    }
  }
}
```

設定ファイルは起動時にだけ読み込まれるため、Claude Desktop を**完全に
終了**してから再度開いてください（ウィンドウを閉じるだけでは反映されま
せん）。

出典: <https://modelcontextprotocol.io/quickstart/user> （取得 2026-06-22）

### Claude Code

公式 CLI で追加するのが最も確実です。スコープを指定したうえで正しい設定
ファイルに書き込まれます。

```bash
claude mcp add --transport stdio --scope user apple-health -- uvx apple-health-mcp-server serve
```

- `--scope user` は全プロジェクト共通の登録です（`~/.claude.json` に書
  き込まれます）。プロジェクト内でチーム共有したいときは `--scope project`
  を指定して `.mcp.json` をリポジトリにコミット、現在のプロジェクトだけ
  で使いたい場合は既定の `--scope local` を選択してください。
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
      "env": {}
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
```

`config.toml` の編集内容は次回 `codex` 起動時に反映されます。実行中の
セッションを再起動してください。`codex mcp list` / `codex mcp get <name>`
/ `codex mcp remove <name>` で確認・削除も行えます。

出典: <https://developers.openai.com/codex/mcp> （取得 2026-06-22）

### データのインポート

ツールから意味のある結果を得るには、最初に一度 Apple Health のエクス
ポートを取り込みます。Apple が解凍したディレクトリ（`export.xml` /
`electrocardiograms/` / `workout-routes/` を含むもの）をそのまま指定し
ます。

```bash
uvx apple-health-mcp-server import /path/to/apple_health_export
```

インポートは冪等です。新しいエクスポートで再実行すると、`import_id`
列により既存データベースに追記される形で統合されます。

Phase 1（XML パース）では 10 秒ごとに進捗ログを 1 行
出力します（`INFO progress: xml NN% (X / Y MB, ~Z min remaining)`）。
ストリーミングで監視している AI エージェントや人間が、数分におよぶ
パース中も処理が進んでいることを確認できます。間隔は環境変数
`APPLE_HEALTH_IMPORT_PROGRESS_SECS`（正の整数、1..600 にクランプ）で
変更でき、静かに走らせたいときは `60`、デバッグ時は `1` などに設定し
ます。1 MB 未満のエクスポートでは進捗行を出力しません。

### データベースの場所

既定では XDG 準拠のデータディレクトリに格納されます。

- Linux / macOS: `~/.local/share/apple-health-mcp/health.duckdb`
- Windows: `%LOCALAPPDATA%\apple-health-mcp\health.duckdb`

両サブコマンドで `--db /custom/path/health.duckdb` を渡せば上書きできます。

### ロケール

Apple Health は ECG CSV のヘッダラベルを iPhone の言語設定に応じてローカライズします
（`export.xml` 自体はロケール非依存です）。 importer がサポートするのは:

- **検証済**: 英語、 日本語 (`記録日` と `記録日時` の両表記)
- **ベストエフォート**: 簡体中文、 繁體中文、 韓国語 — 実際のローカライズ済みエクスポートで
  未検証で、 ラベル文字列は推定値です

どのロケールにもマッチしなかった場合、 警告ログが GitHub Issue Tracker への報告を促します
（CSV の最初の 10 行を添付してください）。 ヘッダ部分には個人情報は含まれません
（`Name` と `Date of Birth` は importer が意図的にスキップします）。

距離・エネルギーの単位 (`km`, `mi`, `kcal`) は HealthKit 識別子由来で、 ローカライズされません。
`workouts` テーブルの `total_distance_unit` カラムに正確に記録されます。

## ツール群

FastMCP に登録される 17 ツールを系統別にまとめます。

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

## トラブルシューティング

**どのツールも `No Apple Health data has been imported yet.` を返す**

ローカルの DuckDB ファイルが空でも MCP サーバーは起動するように
なっており（クライアントから全ツールが見えるようにするため）、
データが必要なツールはインポートが完了するまで上記の案内文を
返します。 以下のコマンドでインポートを実行してください。

```bash
apple-health-mcp-server import /path/to/apple_health_export
```

インポート完了後は **MCP サーバーを再起動** してください
（Claude Desktop / Claude Code / Codex を再起動するか、 `serve`
プロセスを止めて再実行）。 サーバーはプロセス起動時に読み取り
専用の DuckDB スナップショットを掴むため、 新しい行は再接続後
にしか見えません。

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

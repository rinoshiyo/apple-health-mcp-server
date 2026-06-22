<!-- Read this in [English](README.md) / [日本語](README.ja.md) -->

# apple-health-mcp-server

[![PyPI version](https://img.shields.io/pypi/v/apple-health-mcp-server.svg)](https://pypi.org/project/apple-health-mcp-server/)
[![Python versions](https://img.shields.io/pypi/pyversions/apple-health-mcp-server.svg)](https://pypi.org/project/apple-health-mcp-server/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
[![MCP Registry](https://img.shields.io/badge/MCP-Registry-blue.svg)](https://modelcontextprotocol.io/)

> **おそらく、もっとも網羅的な Apple Health MCP サーバー。**
>
> Read this in [English](README.md) / [日本語](README.ja.md).

`apple-health-mcp-server` は、Apple Health の書き出し（`export.xml`
および同梱の ECG CSV / GPX ルートファイル群）を、ローカル
[DuckDB](https://duckdb.org/) データベースを介して任意の
[Model Context Protocol](https://modelcontextprotocol.io/) クライアント
（Claude Desktop を含む）へ公開します。読み取り中心の 16 ツールで構成されます。

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

macOS は `~/Library/Application Support/Claude/claude_desktop_config.json`、
Windows は `%APPDATA%\Claude\claude_desktop_config.json` に以下を追記します。

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

Claude Desktop を再起動してください。ツールから意味のある結果を得るには、
最初に一度データを取り込みます。

```bash
uvx apple-health-mcp-server import /path/to/apple_health_export
```

指定するディレクトリは Apple Health が展開するそのままの形（`export.xml` と
`electrocardiograms/` フォルダ、`workout-routes/` フォルダを含むもの）です。

### データベースの場所

既定では XDG 準拠のデータディレクトリに格納されます。

- Linux / macOS: `~/.local/share/apple-health-mcp/health.duckdb`
- Windows: `%LOCALAPPDATA%\apple-health-mcp\health.duckdb`

両サブコマンドで `--db /custom/path/health.duckdb` を渡せば上書きできます。

## ツール群

FastMCP に登録される 16 ツールは Apple Health 書き出しの主要な切り口を
カバーします。網羅的な一覧は `apple_health_mcp.server.tools`（もしくは
クライアントのツール一覧）を参照してください。大別すると、レコード /
ワークアウト / アクティビティサマリー / 相関 / ECG / ルート /
カスタム SQL / メタデータ系に分類されます。

## 開発

```bash
uv sync
uv run pre-commit install
uv run pytest --cov-branch --cov-fail-under=100
uv run ruff check
uv run ruff format --check
uv run mypy
```

リポジトリ運用の規約（プルリクエストごとの `/code-review --fix` 必須を含む）
は [CLAUDE.md](./CLAUDE.md) を参照してください。

## コントリビューション

Issues / Pull Requests は **英語と日本語のどちらも歓迎** します。
コード内のコメントおよび `docs/`, `README.md`, `CHANGELOG.md`,
`CLAUDE.md`, `SECURITY.md` 配下のドキュメントは、コードベースを統一的に
読める状態に保つために英語で固定しています。`README.ja.md` は唯一の例外です。

## ライセンス

[MIT](./LICENSE)

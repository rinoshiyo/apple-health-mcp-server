<!--
  Minimal stub README. Full content (motivation, install, usage, badges) lands
  with issue #17. This file exists so packaging metadata and the pre-commit
  setup note have a home.
-->

# apple-health-mcp

Probably the most complete Apple Health MCP server.

> Status: pre-alpha. Public API and CLI are not yet stable.

## Development setup

```bash
uv sync
uv run pre-commit install
```

After that, `pre-commit run --all-files`, `uv run pytest`, `uv run ruff check`,
and `uv run mypy` should all succeed.

## License

[MIT](./LICENSE)

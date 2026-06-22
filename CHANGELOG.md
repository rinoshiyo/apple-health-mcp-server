# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-06-22

### Added

- **PyPI Trusted Publishing release workflow.** Pushing a
  `v<MAJOR>.<MINOR>.<PATCH>` tag builds the sdist and wheel, verifies
  metadata with `twine check --strict`, and publishes to PyPI via
  OIDC. The v0.1.0 tag is pushed manually by the maintainer after the
  release branch is merged. (#20)
- **Project bootstrap.** `pyproject.toml` targeting Python 3.12+ with
  `uv`, `ruff`, `mypy --strict`, `pytest --cov-branch --cov-fail-under=100`,
  and `pre-commit` wired up. (#1, #2, #3, #4)
- **DuckDB schema and connection layer** ported from the Rust reference
  implementation, including deterministic deduplication and derived
  daily-stats rebuild. (#9, #10)
- **Importers** for Apple Health XML, ECG CSV (multi-locale), and GPX
  workout routes, orchestrated through
  `apple_health_mcp.importers.run_import`. Time-zone alignment between
  XML (local wall-clock) and GPX (true UTC) is preserved via per-workout
  offsets. (#5, #6, #7, #8)
- **FastMCP server with 16 read-oriented tools**:
  `list_record_types`, `query_records`, `get_record_statistics`,
  `list_workouts`, `get_workout_details`, `get_activity_summaries`,
  `get_workout_route`, `get_heart_rate_samples`, `list_correlations`,
  `get_correlation_details`, `list_ecg_readings`, `get_ecg_data`,
  `run_custom_query`, `list_data_sources`, `get_import_history`,
  `list_state_of_mind`. (#11, #12, #13)
- **Test fixtures and end-to-end integration smoke** under
  `tests/fixtures/` and `tests/integration/`, plus per-matrix-cell
  coverage XML and a single canonical HTML coverage artifact in CI.
  (#14, #15, #16)
- **Documentation**: English and Japanese READMEs, this changelog, and
  `CLAUDE.md` covering architecture, dev commands, conventions, domain
  rules, language policy, release operations, and the mandatory
  `/code-review --fix` policy for every PR. (#17, #18, #19)
- **CLI `import` subcommand** wired to `importers.run_import` so
  `apple-health-mcp-server import <export>` actually ingests data
  before `serve` is started. (#27)

[Unreleased]: https://github.com/rinoshiyo/apple-health-mcp-server/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/rinoshiyo/apple-health-mcp-server/releases/tag/v0.1.0

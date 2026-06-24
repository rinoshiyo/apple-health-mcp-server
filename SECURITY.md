# Security Policy

## Reporting a Vulnerability

Please use [GitHub's Private vulnerability reporting](https://github.com/rinoshiyo/apple-health-mcp-server/security/advisories/new)
to report security issues in `apple-health-mcp-server`. This routes the
report to the repository owner without making it public, lets us
discuss the impact and a fix privately, and assigns a CVE through
GitHub if the issue warrants one.

A GitHub account is required to open a private report. This is the
only intake channel — the project is a single-maintainer effort and
juggling parallel inboxes would introduce more risk than it removes.

## Scope

In scope:

- Vulnerabilities in the importer, server, CLI, or shipped Python
  package dependencies (`pyproject.toml`).
- Vulnerabilities in the release pipeline (`.github/workflows/`) that
  could compromise the published PyPI or MCPB artefacts.
- Data-leak / data-exposure issues in MCP tool responses (e.g.
  `run_custom_query` returning rows the validated SQL gate should
  have blocked).

Out of scope (use normal issues instead):

- Apple Health export schema changes — a new Apple Health record type
  that the importer skips is a feature gap, not a vulnerability.
- Locale / language drift in the friendly-error path — file an issue
  with the locale and the error message.
- Performance regressions — file an issue with a reproducer.

## Response target

Best-effort response within 7 days of the private report being
opened. Single-maintainer project; I'll acknowledge the report,
discuss severity, and propose a timeline in that first response.
Coordinated disclosure timing is negotiable.

## See also

The [Compatibility / Security exception](./README.md#security-exception)
section of the README explains how a CVE-grade flaw in a deprecated
surface can break the normal deprecation cadence — fix-by-removal
shipped in a patch release with a `Security` heading in
`CHANGELOG.md`.

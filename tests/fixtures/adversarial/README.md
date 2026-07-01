# Adversarial ZIP fixtures

Hand-crafted ZIP files for the manual dogfood checklist
(`internal/dogfood/v0-5-1-test-plan.md` §X4). Each fixture exercises one
corner case of the importer / `import_zip` / `list_zips` path that
unit tests can not cover at the same fidelity.

These ZIPs are NOT consumed by `tests/unit/` or `tests/integration/`
— the existing adversarial tests build their own inline ZIPs. The
files here exist so the reviewer can drop them into a real
`APPLE_HEALTH_EXPORT_ZIPS_DIR` and walk the §X4 checks end-to-end via
Claude Desktop or `apple-health-mcp-server` CLI.

## Fixture catalogue

| File | §X4 item | What it targets |
|---|---|---|
| `x4-3-multi-empty-xml.zip` | X4.3 | Multi-entry ZIP whose `apple_health_export/export.xml` is **zero bytes**. Importer should complete (no crash) and produce `record_count: 0`. |
| `x4-4-zip-slip.zip` | X4.4 | Zip-slip attempt: contains an entry named `../../../tmp/zip-slip-escape.txt` plus a valid Apple Health marker. Importer must refuse to write the escape path. After running `import_zip` on this fixture, verify `/tmp/zip-slip-escape.txt` (POSIX) or the Windows equivalent **does not exist**. |
| `x4-5-original.zip` / `x4-5-renamed-clone.zip` | X4.5 | Two filenames, byte-identical contents → identical sha256. Both appear in `list_zips` with the **same `id`** (= `sha256[:8]`). Importing one should mark the other `imported: true` on the next `list_zips`. Pinned sha256 (verified at generation): `b91758aed29a4163655d02d21f1829a859bba8d5cbe242189cab00d86c79ea65` |
| `x4-6-future-mtime.zip` | X4.6 | On-disk mtime stamped to 2100-01-01 UTC. `list_zips` should report the ISO mtime as-is and the sha cache keyed on `(size, mtime)` should treat it like any other entry — no future-date assertion crash. |
| `x4-7-broken-xml.zip` | X4.7 | `export.xml` is XML-syntax-invalid (unclosed `<Record` tag, no closing `</HealthData>`). Importer should advance to Phase-1 XML parse, fail, and land the job in a terminal `error` state with a typed envelope (`reason: run_import_failed` or similar). No raw stack trace. |

## Regenerating

```bash
python tests/fixtures/adversarial/generate.py
```

Deterministic on the byte contents inside each ZIP (the central
directory `ZipInfo` builds from constant strings). The only on-disk
mtime that matters is `x4-6-future-mtime.zip`, which the generator
stamps via `os.utime` after closing the file. Re-running the script
overwrites the existing files in place.

## NOT in this directory

- §X4.1 (= empty ZIP) and §X4.2 (= 1-byte ZIP) are trivial enough
  that the reviewer can produce them inline:

  ```bash
  : > /tmp/x4-1-empty.zip
  printf 'x' > /tmp/x4-2-one-byte.zip
  ```

- §X4.8 (= 5 GB `export.xml`) is environment-dependent (size limits
  vary by reviewer's disk / extraction tooling); not pre-generated.

## Why these fixtures live here, not in `tests/unit/`

The unit / integration suites already cover the corresponding code
paths with synthetic ZIPs built at test time (see
`tests/unit/server/test_zip_tools.py::_make_zip` and
`tests/integration/test_smoke.py`). The fixtures in this directory
exist exclusively for the **manual dogfood checklist** so the
reviewer hits the production wire (Claude Desktop / MCPB bundle)
with real on-disk files, not with in-process bytes.

If a regression were caught here that the unit tests missed, the
correct response is to add a unit-test variant covering the same
shape — not to wire these fixtures into the suite.

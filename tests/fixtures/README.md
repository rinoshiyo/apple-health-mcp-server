# Test fixtures

This directory holds **hand-written, synthetic** Apple Health-shaped data used
by the integration smoke tests in `tests/integration/`.

## Policy

- **No real personal data.** All values (heart rate, step counts, locations,
  ECG samples) are illustrative and were chosen by hand. Nothing here comes
  from anyone's actual Apple Health export.
- **No real device identifiers.** Device-name fields use generic strings like
  `Apple Watch` and `iPhone`. Source names use the same generic vocabulary
  and the literal `Health` (the built-in Apple Health source).
- **No real coordinates from a real route.** GPX coordinates are around
  central San Francisco (37.7749, -122.4194) as a recognisable placeholder.
- **English locale only.** Locale-specific parsing edge cases (Japanese,
  Spanish, etc.) are exercised by inline strings inside the per-importer
  unit tests, not by these on-disk fixtures.
- **Timestamps carry their UTC offset** (`startDate="2024-06-15 08:00:00 +0000"`,
  GPX `<time>2024-06-15T10:00:00Z</time>`). DuckDB's `TIMESTAMPTZ` parser
  normalises both forms to a UTC instant on insert, so the smoke tests can
  pin the session TZ (typically `UTC`) and assert string-formatted equality
  without worrying about which OS the test happens to run on.

These rules keep the fixtures safely committable to a public repository.

## Catalogue

| File | Purpose |
|---|---|
| `sample_export.xml` | Minimal Apple Health export with one of each major element: `ExportDate`, `Me`, `Record` (HeartRate + StepCount + StateOfMind), `Workout` (with `WorkoutEvent`, `WorkoutStatistics`, `WorkoutRoute` referencing the GPX file below), `ActivitySummary`, and `Correlation` (blood pressure pair). |
| `sample_ecg.csv` | Minimal English-locale ECG export with all standard header rows and ten voltage samples. |
| `sample_workout_route.gpx` | Minimal three-point GPX track corresponding to the `WorkoutRoute` referenced from `sample_export.xml`. Includes the Apple-style `<extensions>` block (`speed`, `course`, `hAcc`, `vAcc`). |

## Regeneration

The fixtures are tiny enough to maintain by hand. To regenerate or extend
them:

1. Open the file in this directory.
2. Add or modify elements following the shapes already present.
3. Re-run `uv run pytest tests/integration/` to confirm the importer still
   accepts the input and the smoke assertions hold.

Do **not** copy-paste from a real `export.xml`, real ECG CSV, or real GPX
trace. Always synthesise the values by hand.

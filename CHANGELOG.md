# Changelog

All notable changes to this project will be documented in this file.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Version and compatibility policy: [Releasing](docs/releasing.md).

## [Unreleased]

### Added

- `convert`, `bootstrap-csv`, `run-all`, `run-daily`, `publish` and `run-etl` (which writes the
  provider's source Parquet) hold an exclusive lock on the provider for the whole command, in `.<name>.write.lock` beside the provider. When another
  process holds it, the command exits 75 at once and leaves the provider unchanged.
- `jqqlib.provider_lock.provider_read_lock` lets a reader copy a provider while every writer
  command stays out. `provider_write_lock` gives library callers the same lock as the CLI.
- `publish` also holds a shared lock on `--build-provider-uri`, so it refuses to publish a build
  that a writer is still writing.
- A provider that is a symlink is locked both by its name and by the tree it resolves to, so a
  reader and a writer that use different paths for the same tree still exclude each other.
- Commands that write the Parquet also lock `storage.parquet_dir` exclusively, and `convert` locks it
  shared, so two configs with different `qlib_dir` cannot write the same Parquet at once.
- After taking every lock, the lock files are resolved again and taken again if `publish` moved a
  symlink in between. A `..` after a symlink locks the directory the OS opens. The lock is host-local.
  The symlink `publish` swaps must be the last component of the provider path, not a symlink above
  it or in a chain of symlinks.

## [0.4.0] - 2026-10-26

### Changed

- Require Python >=3.11,<3.13, pandas >=3.1,<4, NumPy >=2.0.2 and PyArrow >=16.
- Test pandas 3.1.0 and latest stable 3.x on Python 3.11 and 3.12.
- Preserve existing Parquet upsert and Qlib output contracts during migration.
- Add an offline migration probe covering legacy upsert, missing/blank/alphanumeric symbols,
  corrections, duplicate rows, split/rights adjustments, failed-write preservation, and PIT
  consistency across second/millisecond/microsecond/nanosecond calendars.

Python 3.10 and pandas 2 users must upgrade their Python and dependencies. CLI, configuration,
JSON schemas and Qlib fields are unchanged. Existing Parquet files continue with ordinary
upsert; do not regenerate history. Parquet metadata/hash may change once after upgrading,
causing one additional publish. Validate and audit before publishing; replaying identical
input must be a no-op. Roll back the package and its dependency environment together; do not
automatically roll back market data or the publish ledger.

### Fixed

- Every Qlib float32 bin now equals the direct float32 conversion of the adjusted float64
  values. pandas 2's CSV parser misread values that fall near a float32 rounding midpoint,
  which put some volume values one float32 step (up to 8 shares) off. The affected bins
  change once after upgrading.

## [0.3.1] - 2026-10-26

### Security

- Validate symbol filenames before storing or converting quotes, including raw adjustment mode.
  Reject unsafe paths and symbols that collide after Qlib filename normalization before writing files.
- Audit resolved dependencies in pull requests, weekly, and before releases.
- Require patched HTTP clients and MLflow, preventing resolution backtracking to obsolete MLflow releases.

## [0.3.0] - 2026-10-19

### Added

- `run-daily` and `audit` accept `--result-json PATH` and write
  `{"schema_version": 1, "command", "outcome", "exit_code", "error"}` to `PATH` (temporary file and atomic
  replace) on success, on audit gaps, when the XTKS calendar is unavailable and on every `error:` exit.
  `outcome` is `completed`, `gaps` (audit only), `calendar_unavailable` or `failed`, so a wrapper can tell
  these apart without reading stderr. The schema is `jqqlib.contracts.load_schema("cli_result")` and also
  checks that `command`, `exit_code` and `error` match the `outcome`. Argument
  errors write no file; a failed write only adds a warning and keeps the exit code. Without the flag the
  output and exit codes are unchanged.

### Changed

- The fundamentals raw cache is one file. `fundamentals_raw_cache.parquet` now records the queried codes
  (including codes with no disclosures) in its Parquet footer, written in the same atomic replace as the
  rows, so a run killed at any point resumes with no duplicate or lost codes.
  `fundamentals_raw_cache_attempted.json` is no longer used. The first run after upgrading converts a cache
  written by 0.2.0 or earlier and deletes that file. Before downgrading to 0.2.0, delete
  `fundamentals_raw_cache.parquet`: 0.2.0 ignores the footer, queries every code again and appends the
  responses to the rows already in the file, which leaves every code's rows duplicated.

### Removed

- The Python functions `build_dataset_manifest`, `build_dataset_quality_report`, `write_dataset_manifest` and
  `write_dataset_quality_report` in `jqqlib.manifest`. No command used them. `write_dataset_artifacts` writes
  both files from one quality inspection; `read_dataset_manifest` and `read_dataset_quality_report` still read them.

### Fixed

- `run-fundamentals-etl` no longer duplicates a code's rows when a run is killed between the two cache
  writes of 0.2.0 and earlier. Converting an existing cache also collapses rows that this bug had already
  duplicated, without querying those codes again.
- A code listed twice in the codes passed to `fetch_fundamentals` is queried once.
- Two consecutive runs over the same codes now return equal frames, also when a column is null for only
  some codes.

## [0.2.0] - 2026-10-16

### Added

- `publish_check.baseline: file` compares `publish-check` against the last entry of a JSON Lines
  publish ledger at `publish_check.ledger_path` instead of the manifest committed on `git_ref`, so
  an operations repository no longer has to commit and push the manifest. The default stays `git`;
  one baseline is never a fallback for the other. A missing or damaged ledger is an error (exit 1),
  not a first publish; an empty ledger is.
- `jqqlib ledger init [--from-manifest PATH]` creates the ledger once (empty, or seeded from a
  manifest such as the last one published by the git baseline), and `jqqlib ledger append` records
  the current dataset after a successful publish. `append` runs the `publish-check` dataset checks,
  rewrites the ledger atomically under a lock and appends nothing when the last entry already
  records the dataset. Entries carry `jqqlib_version` and, when the package is the `src/jqqlib`
  directory of a jquants-qlib work tree, `jqqlib_source_commit`; neither takes part in the change
  decision. Symlinked ledgers and entries with missing fields are refused.

### Fixed

- API retries no longer repeat 4xx client errors: `requests.HTTPError` subclasses `OSError`, so
  401/403/404 (and the 400 that triggers the per-day fallback) were retried with backoff. Errors
  carrying an HTTP status are now classified by status first; only 429/5xx are retried.
- `run-daily` refuses `etl.merge_mode: replace` (exit 2), which cut the Parquet down to the daily
  fetch window on every run.
- Parquet writes (`run-etl`, `run-all`, `run-daily`, `bootstrap-csv`) run the `validate` readiness
  checks on the merged rows first and leave the Parquet unchanged on failure (exit 1). Previously
  rejected rows (e.g. `AdjFactor` 0) were written before the dump refused them, so `run-daily`
  resumed after them and failed on every later run.
- `publish-check` reports an error (exit 1) instead of "changed" (exit 0) when it cannot look up
  the committed manifest: `git` missing, not a git work tree, or a `publish_check.git_ref` that
  does not name a commit. It also maps the manifest path to the repository root, so an absolute
  `storage.qlib_dir` or a run from a subdirectory no longer reports "changed" on every run.
- The Qlib subtree swap restores the previous `features` / `calendars` / `instruments` when it is
  interrupted (e.g. Ctrl-C), and keeps the backups if the restore itself fails; previously an
  interrupt deleted them.
- The Qlib dump stages its build next to the resolved `storage.qlib_dir`, so a `qlib_dir` that is
  a symlink to another filesystem no longer fails with `Invalid cross-device link`.
- Fundamentals disclosures of one code that take effect on the same day (e.g. results and a
  forecast revision on one `DiscDate`) are merged field by field in `DiscDate` / `DiscTime` (as a
  time of day) / `DiscNo` (as an integer) order, latest non-null value first; a missing or
  unparsable key counts as the earliest. Previously one whole row was kept, chosen by input
  order, so the new `NP` or the new `FNP` stayed stale until the next disclosure. Re-run
  `run-fundamentals-etl` to rebuild the panel.
- With `qlib.adjustment: vendor_factor`, the quality report fails (`dumped_factor`) when the
  factor cannot be computed (no `adjustment_factor` column, or null factors) instead of skipping
  the check and reporting `pass`.
- `publish-check` also compares the manifests' `adjustment_policy`, so switching
  `qlib.adjustment` (same Parquet, different Qlib dataset) is a change, not "unchanged".
- `dataset_id` includes the adjustment mode, so the same Parquet dumped in both modes gets two
  ids. Existing ids change once on the next build.
- Building the manifest and quality report reads the Parquet and runs the quality inspection once
  instead of twice, and the vendor-factor transform is vectorised (about 8x faster on 1M rows,
  same values and row order; a nullable integer factor is computed in float64).

### Changed

- **Breaking (missing vendor factor):** with `qlib.adjustment: vendor_factor` (the default),
  `run-etl` / `run-all` / `run-daily` / `bootstrap-csv` refuse source data that has no `AdjFactor`
  / `AdjustmentFactor` column (`error: Source data has no AdjFactor/AdjustmentFactor column...`,
  exit 1, Parquet unchanged). Previously every row silently got factor 1.0, so splits were
  dumped unadjusted. Null or non-numeric factors on individual rows still become 1.0, now with a
  `Filled N null or non-numeric AdjFactor values with 1.0.` warning on stderr; with
  `qlib.adjustment: none` a source without the column still gets 1.0, with a warning.
  - Affected: `bootstrap-csv` from a CSV without a factor column (e.g. a hand-made or third-party
    CSV) in the default mode. J-Quants API responses and `download-csv` CSVs carry the column.
  - Required changes: re-download the CSV with `download-csv`, add the vendor factor column, or
    set `qlib.adjustment: none` for raw prices.
  - Data: existing Parquet is unchanged; it may already hold 1.0 factors from such a source, so
    rebuild it from a source with factors if split adjustment matters.
  - Validate / roll back: `jqqlib bootstrap-csv` then `jqqlib validate` exit 0; rolling back to
    the previous release restores the silent 1.0 fill.

- **Breaking (CLI exit codes):** J-Quants API / network errors (`requests` exceptions), no data
  (within the `run-daily` lookback, or no fundamentals), and unreadable Parquet now exit 1 (failed
  run) instead of 2. Exit 2 is left to configuration and argument errors, rejected credentials
  (HTTP 401/403), and the XTKS calendar outage of `run-daily` / `audit`, so a wrapper can treat 2
  as "fix the setup" and 1 as "the run failed". The fundamentals CLI follows the same mapping and
  reports runtime errors as one `error:` line instead of a traceback. `publish-check` now exits 1
  where it used to exit 0 when the committed manifest cannot be looked up (see Fixed).
  - Affected: wrappers or monitors that branch on exit code 2 for API outages or "no data", or
    that rely on `publish-check` exit 0 with a missing or unfetched `publish_check.git_ref`.
  - Required changes: alert on exit 1 for those cases; make sure `publish_check.git_ref` exists
    (fetch it, or push an initial commit before the first publish).
  - Data: no regeneration needed; Parquet, Qlib datasets and artifacts are unchanged.
  - Validate / roll back: `jqqlib publish-check --config config.yaml; echo $?` should print 0 or 3
    on a healthy setup. Rolling back to 0.1.0 restores the previous codes.

## [0.1.0] - 2026-09-16

### Added

- Initial public release: J-Quants Light daily ETL to deterministic raw Parquet and Qlib datasets,
  bulk-CSV bootstrap, incremental fetch with retry/backoff, self-audit of recent business days,
  atomic symlink publish of a prepared Qlib provider, and JSON manifests / quality reports.
- Qlib `$factor` in Qlib's convention, computed at dump time from the vendor
  `AdjFactor` / `AdjustmentFactor` column with the official cumulative rule
  (`qlib.adjustment: vendor_factor`, the default): Qlib prices are multiplied and volume divided
  by the cumulative factor, `$factor` is 1.0 on each symbol's latest date, and
  `$close / $factor` restores the raw close. Rights issues (`ex_rights_type` 3) adjust prices but
  not volume; dividends are not adjusted. Parquet stays raw and gains a required
  `adjustment_factor` column (optional `ex_rights_type`). `qlib.adjustment: none` dumps the six
  raw Qlib fields with no `factor` file.
- Manifest `field_schema` / `adjustment_policy` describe the applied factor (`source:
  J-Quants AdjFactor`), and the quality report checks the dumped `factor` column.
- Synthetic fixture option `--split CODE:DATE:FACTOR` for split scenarios;
  `scripts/check_qlib.py` reads `$factor` and omits it when the provider has no factor file.
- The default (`vendor_factor`) dump refuses Parquet that is not Qlib-ready (missing, non-finite
  or non-positive `adjustment_factor`, missing OHLCV columns) with
  `error: Parquet is not Qlib-ready: ...` and exit 1, and never writes infinite volumes;
  `validate` remains the standalone check and `scripts/run_daily.sh` still runs it after
  `run-daily`.
- Bulk CSV pass-through keeps `ExRT`, so `download-csv` → `bootstrap-csv` Parquet carries
  `ex_rights_type` and rights-issue rows dump volume unscaled.
- `THIRD_PARTY_NOTICES.md` (shipped via `license-files` and the sdist) attributes
  `src/jqqlib/qlib_dump.py` to microsoft/qlib `scripts/dump_bin.py` (MIT).
- GitHub Actions are pinned to commit SHAs with `# vX.Y.Z` comments (Dependabot refreshes them);
  the CI `dist` job asserts sdist members and runs the unittest suite from the extracted sdist;
  `scripts/dist_smoke.py` checks the `factor` binary and `$factor` = 1.0 on the last date.
- Branch model: `dev` is the default/integration branch; a merge into `master` runs the release
  workflow, which releases `vX.Y.Z` from `pyproject.toml` when the tag is absent and the
  CHANGELOG has a `## [X.Y.Z]` section (creating the tag with the GitHub Release). CI and CodeQL
  run on `dev` and `master`; Dependabot targets `dev`.
- `publish-check` compares against the configurable `publish_check.git_ref` (default
  `origin/main`) instead of a hard-coded branch.
- `scripts/run_daily.sh` exports `JQQLIB_RUN_STARTED_AT`; `JQQLIB_CONFIG`, `JQQLIB_PYTHON` and
  `JQQLIB_RUN_STARTED_AT` are documented in the configuration guide.
- Fundamentals PIT audit failures raise `PitAuditError` (CLI exit 1) instead of `SystemExit`;
  `python -m jqqlib.fundamentals_pipeline` is the argparse prog name; every module has a docstring;
  manifest `factor` dtype is `float32`.
- `docs/jquants-price-adjustment.md`: what the official J-Quants documentation states about
  adjusted prices (adjustment factor, cumulative rule, retroactive recalculation, corrections)
  and how the implemented `$factor` follows it.
- `jqqlib` console script (also `python -m jqqlib`) with `--version`, and `jqqlib init-config`
  to write the packaged example configuration for pip installs.
- Packaged manifest and quality-report JSON schemas with `jqqlib.contracts.load_schema`.
- Configurable `storage.audit_dir` for the audit sidecar (default `./data/audit`).
- `jqqlib.__version__` sourced from package metadata, and a `py.typed` marker in the wheel.
- Development extras with Ruff, mypy, build, Twine, and coverage tooling.
- CI lint/type checks, distribution builds, sdist rebuilds, and installed-wheel smoke checks.
- Tag-triggered release workflow with PyPI trusted publishing and GitHub Release artifacts whose
  notes are the matching CHANGELOG section.
- Release and compatibility policy, PR checklist, `CITATION.cff`, and grouped weekly dependency
  updates.
- `CODE_OF_CONDUCT.md` (Contributor Covenant 2.1), issue-template contact links for private
  vulnerability reporting, and a details-free `[security]` issue template as the fallback.
- `MANIFEST.in` so the sdist carries `docs/`, the changelog, community files,
  `config.example.yaml`, and the complete `tests/` and `scripts/` trees, so the unittest suite
  runs from an extracted sdist.
- CodeQL workflow (`security-and-quality` queries, weekly schedule) and an `.editorconfig`.
- End-to-end CLI test mirroring the README synthetic-data quick start.
- Operations guide section on failure recovery (re-fetch, failed publish, missing audit sidecar,
  calendar unavailable `rc=2`).

### Changed

- `storage.parquet_dir` and `storage.qlib_dir` are required; loading a config without
  them raises `ValueError("storage.<key> is required.")`.
- CLI configuration and argument errors exit with code 2 and `error: <message>` on stderr
  instead of a traceback.
- Distribution smoke (`scripts/dist_smoke.py`) runs without `PYTHONPATH` from a temporary
  directory against both the original wheel and the sdist-rebuilt wheel, each in its own venv,
  drives the `jqqlib` console script, and checks `--version` and `py.typed`.
- Audit sidecar output is selected through configuration instead of the source checkout.
- README and Japanese guides reorganized around setup, configuration, operations, architecture,
  and experimental fundamentals; the README now leads with installation and the
  API-key-free synthetic example, followed by the real-data run.
- Contribution and security guidance describe validation and support expectations; mypy checks
  all of `src/jqqlib` (`scripts/` and `tests/` are not yet type-checked).
- Supported platforms are documented as POSIX only (macOS / Linux).
- Ruff line-length rule (E501, 120 columns) is enforced instead of ignored.
- `.gitignore` adds `.coverage`, `coverage.xml`, `htmlcov/`, `.mypy_cache/`, and `.omc/`.
- Example config and daily wrapper comments point to `docs/configuration.md` for API-key setup.
- `exchange_calendars` is constrained to `>=4.13.2,<5` instead of an exact pin, so the package
  can coexist with other tools in one environment.
- `scripts/check_qlib.py` samples the last day in the dataset calendar unless `--date` is given,
  instead of a hard-coded date.
- Example config lists a commented-out experimental `fundamentals:` block.
- README adds a status / stability statement, related projects, the complete exit-code summary,
  and a `qlib.init` example for reading the generated dataset.
- Development extra requires `twine>=6.1`, the first version that validates the Metadata 2.4
  (`License-Expression`) that `setuptools>=77` emits.

- `scripts/run_daily.sh` resolves the CLI from `JQQLIB_PYTHON`, the checkout's `.venv`, or
  `jqqlib` on `PATH`, instead of requiring the checkout's `.venv`.
- Release workflow jobs only run in the upstream repository, and the PyPI publish action is
  pinned to a commit SHA. PyPI publishing is opt-in through the repository variable
  `PUBLISH_TO_PYPI`; without it a `v*` tag produces a GitHub-only release with the sdist and wheel
  attached.
- `.gitignore` covers the upstream client's `jquants-api.toml` / `.jquants-api/` credential files.
- The missing-credential error explains that a non-empty `jquants.api_key` /
  `jquants.refresh_token` in `config.yaml` is ignored without `JQQLIB_ALLOW_YAML_CREDENTIALS=1`.
- Library progress in `jqqlib.pipeline` goes through `logging` (`INFO` for daily catch-up
  progress). The `jqqlib` CLI still prints the same lines on stdout (warnings on stderr);
  importing the library attaches no handler.
- `--as-of today` for `run-daily` and `audit` is the current date in Asia/Tokyo, not the host
  local date.
- macOS Keychain service names (`JQQLIB_KEYCHAIN_API_KEY_SERVICE` /
  `JQQLIB_KEYCHAIN_REFRESH_TOKEN_SERVICE`) are read at call time instead of import time, so an
  override set after import takes effect.
- Test temp directories are registered with `addCleanup` / `addClassCleanup` so they are removed
  even when a test fails, and the dump test restores qlib's process-global logging and config.
- Contributor docs describe the sdist-shipped `tests/` and `scripts/` trees, `CI=1` failing when
  the `jqqlib` console script is missing, the local uv Python 3.10 command, `.editorconfig`, and
  the CodeQL workflow; the operations guide notes that `run-daily` accepts and ignores
  `--window-business-days`.

### Fixed

- Manifest timestamps used `datetime.UTC`, which only exists on Python 3.11+; Python 3.10 now
  works as advertised (`datetime.timezone.utc`).
- YAML syntax errors in the configuration file are reported as `error: invalid YAML in <path>: ...`
  with exit 2 instead of a traceback; the same boundary now covers
  `python -m jqqlib.fundamentals_pipeline`.
- `audit` no longer prints an empty `suppressed (XTKS non-session):` header.

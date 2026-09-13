# Changelog

All notable changes to this project will be documented in this file.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Version and compatibility policy: [Releasing](docs/releasing.md).

## [Unreleased]

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

# Contributing

Thank you for your interest in jquants-qlib. Issues and pull requests are welcome in Japanese or English.
Participation is governed by the [Code of Conduct](CODE_OF_CONDUCT.md).

## Development setup

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
python -m unittest discover -s tests
jqqlib --help
jqqlib --version
ruff check .
mypy
```

`jqqlib` is the console script installed by the package; `python -m jqqlib` is equivalent.
`jqqlib init-config [PATH]` writes the packaged example configuration (default `./config.yaml`,
`--force` to overwrite). For a checkout, `cp config.example.yaml config.yaml` does the same; the
root file and `src/jqqlib/data/config.example.yaml` must stay byte-identical, and a unittest
enforces it, so edit both when you change a setting.

Tests need neither a J-Quants API key nor a `config.yaml`. A few tests are skipped when `git` is not available.
When `CI` is set (for example `CI=1`), `tests/test_cli.py` fails instead of skipping if the
`jqqlib` console script is missing — that is a packaging regression, not an optional skip.

The project supports POSIX platforms only (macOS and Linux): dataset publishing relies on
symlinks and the credential fallback uses the macOS Keychain. Windows is not a target.

To reproduce the CI Python 3.10 leg locally with [uv](https://docs.astral.sh/uv/):

```bash
uv venv --python 3.10 /tmp/jqqlib-py310 && uv pip install --python /tmp/jqqlib-py310/bin/python -e ".[dev]"
/tmp/jqqlib-py310/bin/python -m unittest discover -s tests
```

mypy checks all of `src/jqqlib`; `scripts/` and `tests/` are not yet type-checked. CI runs the same
configuration (job `lint + type-check`). `[tool.mypy] files = ["src/jqqlib"]` in `pyproject.toml`
picks up new modules automatically. Third-party packages without stubs are listed per module under
`[[tool.mypy.overrides]]` with `ignore_missing_imports = true`; when you add a dependency and mypy
reports a missing stub, add that module family to the list rather than a global ignore. `ruff check .`
covers the whole tree, including line length. `.editorconfig` sets charset, newlines, and indent
for this repository. CI also runs CodeQL (`security-and-quality` queries) on the Python tree.

Coverage is not gated in CI. To measure it locally:

```bash
coverage run -m unittest discover -s tests && coverage report
```

## Ground rules

- **Never commit market data or credentials.** `data/`, `downloads/`, `config.yaml` and `.envrc` are gitignored on purpose. The J-Quants terms of service do not allow redistributing the data.
- Keep the pipeline deterministic: identical inputs must produce byte-identical Parquet and the same `lineage.parquet_sha256`.
- Changes to `dataset_manifest.json` / `dataset_quality_report.json` must keep `src/jqqlib/contracts/*.schema.json` in sync with the code. `tests/test_contracts_package.py` checks the packaged schemas load, and `tests/test_manifest.py` validates a generated manifest against them; update both when the schema changes. Bump `schema_version` for breaking changes.
- Add or update a unittest for every behaviour change. The suite is `unittest`, not pytest.
- Keep pull requests focused. Describe *why* in the PR body and link the issue if one exists.
- Target the `dev` branch (the repository default). `master` only receives release merges from
  `dev`; a merge into `master` triggers the release workflow (see [releasing](docs/releasing.md)).

## Reporting a bug

Use the bug-report issue template and include the exact command, the pipeline output (redact anything from your API responses you are not allowed to share), and your `python --version` / `pip freeze | grep -iE "pandas|pyarrow|pyqlib|jquants"`.

For a suspected vulnerability do **not** open a regular issue. Follow [SECURITY.md](SECURITY.md);
while private vulnerability reporting is not enabled, use the `security_report` issue template
(title prefix `[security]`) and state only that you have a report and how to reach you — no details.

## Distribution checks

Use Python 3.10–3.12. Build from a checkout whose `dist/` has no older artifacts:

```bash
python -m build
python -m twine check dist/*
repo_dir="$PWD"
dist_tmp="$(mktemp -d)"
python -m pip wheel dist/*.tar.gz --no-deps -w "$dist_tmp/rebuild"
python -m venv "$dist_tmp/venv-wheel"
"$dist_tmp/venv-wheel/bin/python" -m pip install "$repo_dir"/dist/*.whl
python -m venv "$dist_tmp/venv-sdist"
"$dist_tmp/venv-sdist/bin/python" -m pip install "$dist_tmp"/rebuild/*.whl
(cd "$dist_tmp" && "$dist_tmp/venv-wheel/bin/python" "$repo_dir/scripts/dist_smoke.py")
(cd "$dist_tmp" && "$dist_tmp/venv-sdist/bin/python" "$repo_dir/scripts/dist_smoke.py")
```

Run the smoke without `PYTHONPATH` pointing at the checkout. It verifies the installed `jqqlib`
console script (`jqqlib --version` must equal the installed distribution version), `py.typed`, both
packaged schemas, and a CSV → Parquet → Qlib conversion using only the installed wheel. CI runs
exactly this for both the original wheel and the wheel rebuilt from the sdist.
The sdist ships the full `tests/` and `scripts/` trees (`graft tests` / `graft scripts` in
`MANIFEST.in`). After extracting it, `python -m unittest discover -s tests` must pass. The wheel
still omits `tests/`, so the smoke uses an inline CSV fixture for that path.
CI's `dist` job also asserts sdist members (`tests/`, `scripts/`, `docs/`, `CHANGELOG.md`,
`config.example.yaml`, `THIRD_PARTY_NOTICES.md`) and runs `python -m unittest discover -s tests`
inside the extracted sdist (fresh venv with `.[dev]` from that sdist, or the rebuilt wheel plus
the sdist's tests).
See [releasing](docs/releasing.md) for the complete release and compatibility policy. When bumping
the version, update `[project].version` in `pyproject.toml` and `version` in `CITATION.cff` together.

## Synthetic fixtures

The helper in `tests/support/synthetic.py` has the API
`write_synthetic_daily_quotes_csv(path, *, symbols, start, end, events=None) -> Path` and writes
reproducible J-Quants-shaped CSV using invented quotes. Optional `events` maps symbol →
`{date: factor}` and writes an `AdjustmentFactor` column (`1` on other rows). The CLI flag
`--split CODE:DATE:FACTOR` is repeatable. Default output is unchanged (no extra column), so the
README quick start and the inline fixture in `scripts/dist_smoke.py` stay byte-identical.
Generate an example without credentials using:

```bash
python -m tests.support.synthetic --out /tmp/sample
python -m tests.support.synthetic --out /tmp/sample --split 10010:2026-06-03:0.5
```

Use synthetic inputs in regression tests; never copy real API responses into fixtures.
The installed-wheel smoke ships its own inline fixture with the same column contract, because
the wheel does not include `tests/` (the sdist does); the helper above is exercised by the
unittest suite.

## Terminology

- Dataset **publish**: activate a validated Qlib provider, as described in [operations](docs/operations.md).
- Package **release**: distribute a version of `jquants-qlib` through PyPI and GitHub Releases.

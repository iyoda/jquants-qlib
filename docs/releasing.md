# Releasing jquants-qlib

Package **release** means distributing Python artifacts to PyPI and GitHub Releases.
Dataset **publish** means switching the active Qlib provider; see [operations](operations.md).
The CLI is the supported surface. The Python API does not yet carry a stability guarantee.

## Branch model

- `dev` is the integration branch and the repository default: every feature, fix and Dependabot
  pull request targets `dev` and is merged there after CI passes.
- `master` only receives release merges from `dev`. A merge into `master` **is** the release:
  `release.yml` runs on every push to `master`, reads `[project].version` from `pyproject.toml`,
  and releases `vX.Y.Z` when that tag does not exist yet **and** `CHANGELOG.md` has a
  `## [X.Y.Z]` section. Otherwise the run is a no-op (with a warning when the section is missing),
  so merging docs-only changes into `master` is safe.
- Nobody pushes tags by hand; the workflow creates the tag together with the GitHub Release.

## Release destinations

PyPI publishing is **opt-in**. The `pypi` job of `release.yml` runs only when the repository
variable `PUBLISH_TO_PYPI` is `"true"`; otherwise it is skipped and a pushed `v*` tag produces a
GitHub Release with the built sdist and wheel attached (GitHub-only release). Users install a
GitHub-only release with:

```bash
pip install "git+https://github.com/iyoda/jquants-qlib@vX.Y.Z"
```

The GitHub Release needs one repository setting regardless of PyPI: Settings → Actions →
General → Workflow permissions must allow the `contents: write` permission the
`github-release` job requests.

## One-time maintainer setup (PyPI, when you decide to publish there)

Complete this checklist, then set `PUBLISH_TO_PYPI` to `"true"` under Settings → Secrets and
variables → Actions → Variables. If the variable is set before the steps below are done, the
`pypi` job fails (no trusted publisher accepts the OIDC token) and the `github-release` job is
blocked for that run:

1. On PyPI, add a trusted publisher for `jquants-qlib` with the repository owner, repository
   name, workflow filename `release.yml`, and environment name `pypi`. For the first
   publication, use a *pending* publisher because the project does not exist yet.
   No long-lived PyPI API token is needed; the publish job requests `id-token: write`.
   See the [PyPI trusted publishing documentation](https://docs.pypi.org/trusted-publishers/using-a-publisher/).
2. On GitHub, create the environment `pypi` (Settings → Environments) and restrict its
   deployment branches to `master`. Add required reviewers if you want a manual approval gate.

If a release runs with `PUBLISH_TO_PYPI` set but the setup incomplete, fix the setup and re-run
the failed jobs of the same workflow run; do not merge again.

## Release procedure

All preparation happens on a branch off `dev` (a "release PR"), reviewed and merged into `dev`
first, then `dev` is merged into `master`.

1. Update `[project].version` in `pyproject.toml` to `X.Y.Z`, and in `CITATION.cff` set
   `version` to match and `date-released` to the release date (`YYYY-MM-DD`).
2. In [CHANGELOG](../CHANGELOG.md), rename the `## [Unreleased]` heading to `## [X.Y.Z] - YYYY-MM-DD`
   and open a new empty `## [Unreleased]` section above it. Nothing has been tagged or published
   yet, so the first release `v0.1.0` turns the current `Unreleased` section into `[0.1.0] - <date>`.
   The release workflow releases only when this heading exists and uses exactly that section as
   the GitHub Release notes. Include compatibility and migration instructions for breaking changes.
3. In a Python 3.10–3.12 development environment, run:

   ```bash
   python -m pip install -e ".[dev]"
   python -m unittest discover -s tests
   jqqlib --version
   jqqlib --help
   ruff check .
   mypy
   ```

   `jqqlib --version` must print `jqqlib X.Y.Z` for the version you just set.

4. Build from a clean checkout so `dist/` contains only the intended version. Verify the artifacts:

   ```bash
   python -m build
   python -m twine check dist/*
   tar tzf dist/*.tar.gz | grep -E "docs/|CHANGELOG.md|CODE_OF_CONDUCT.md|config.example.yaml|tests/support/|scripts/"
   repo_dir="$PWD"
   release_tmp="$(mktemp -d)"
   python -m pip wheel dist/*.tar.gz --no-deps -w "$release_tmp/rebuild"
   python -m venv "$release_tmp/venv-wheel"
   "$release_tmp/venv-wheel/bin/python" -m pip install "$repo_dir"/dist/*.whl
   python -m venv "$release_tmp/venv-sdist"
   "$release_tmp/venv-sdist/bin/python" -m pip install "$release_tmp"/rebuild/*.whl
   (cd "$release_tmp" && "$release_tmp/venv-wheel/bin/python" "$repo_dir/scripts/dist_smoke.py")
   (cd "$release_tmp" && "$release_tmp/venv-sdist/bin/python" "$repo_dir/scripts/dist_smoke.py")
   ```

   The `tar tzf` line confirms that `MANIFEST.in` put the guides, changelog, community files,
   `config.example.yaml`, and the complete `tests/` and `scripts/` trees into the sdist (the
   unittest suite must pass from an extracted sdist). The smoke drives the installed `jqqlib` console
   script (resolved next to the venv's interpreter), checks that `jqqlib --version` matches the
   installed distribution metadata, that the `py.typed` marker is packaged, that
   `jqqlib init-config` writes the packaged example configuration, and that both packaged JSON
   schemas load; then it converts a small synthetic CSV → Parquet → Qlib and asserts row count,
   calendar, instruments, feature files, and schema validity of the manifest and quality
   report. Run it **without** `PYTHONPATH`: the package must resolve from the
   installed wheel (source lives under `src/`), and the wheel does not ship `tests/`, so the
   smoke uses its own inline CSV fixture. That inline path is exactly what CI exercises; the
   richer `tests/support/synthetic.py` helper is covered by the unittest suite. CI's `dist` job
   runs the smoke twice, in separate virtual environments: once for the wheel from
   `python -m build` and once for the wheel rebuilt from the sdist.

5. Merge the release PR into `dev` and confirm all CI jobs pass there. Then open a pull request
   from `dev` to `master` and merge it (a merge commit or fast-forward; do not squash away the
   history). No tag is pushed by hand.

6. The push to `master` runs the release workflow: it resolves `vX.Y.Z` from `pyproject.toml`,
   confirms the tag does not exist and the `## [X.Y.Z]` CHANGELOG section does, checks the
   installed package version against that tag, extracts the section into the release notes, runs
   tests/lint/type checks, builds and validates artifacts, and smokes both the built wheel and
   the sdist-rebuilt wheel in fresh virtual environments without `PYTHONPATH`, exactly like CI.
   Third-party GitHub Actions are pinned to commit SHAs with a `# vX.Y.Z` comment; Dependabot
   keeps the pins current.
   When `PUBLISH_TO_PYPI` is `"true"`, the `pypi` environment job publishes those artifacts;
   otherwise it is skipped. The final job creates the GitHub Release (which creates the `vX.Y.Z`
   tag on the merged commit) from the extracted notes and attaches the same files. Inspect every
   destination you use. A GitHub Release failure after PyPI succeeds does not undo the PyPI
   upload; rerun only failed jobs. Never replace an already published version: a later merge into
   `master` with the same version is a no-op because the tag exists.
7. Check installation in another fresh environment, without a checkout on `PYTHONPATH`. For a
   GitHub-only release install from the tag; from PyPI, install the pinned version:

   ```bash
   verify_tmp="$(mktemp -d)"
   python -m venv "$verify_tmp/venv"
   "$verify_tmp/venv/bin/python" -m pip install "git+https://github.com/iyoda/jquants-qlib@vX.Y.Z"
   # or, when published to PyPI: ... -m pip install jquants-qlib==X.Y.Z
   (cd "$verify_tmp" && "$verify_tmp/venv/bin/python" "$repo_dir/scripts/dist_smoke.py")
   ```

## Compatibility and migration policy

| Version | When to change it | Dataset migration |
| --- | --- | --- |
| Package `X.Y.Z` | Every release merge from `dev` into `master`; patch for compatible fixes, minor for new features. During `0.x`, breaking CLI changes require a minor bump and explicit notes; from `1.0`, use a major bump. | Packaging/docs changes alone do not require regeneration. State the effect of behavioral fixes. |
| Artifact `schema_version` | Incompatible changes to manifest or quality-report structure, required fields, or field meaning; update the affected schema and consumers together. | Regenerate affected artifacts or document a validated metadata migration. Rebuild datasets only if their content contract changes. |

Schemas live in `src/jqqlib/contracts/` and are loaded with `jqqlib.contracts.load_schema`.
A single change may require more than one version bump. Every breaking-change note must identify
who is affected, old/new behavior, required config or command changes, whether data regeneration
is needed, and how to validate and roll back the migration. See [operations](operations.md) for
dataset publication details.

## Summary

Base branch: `dev` (only release pull requests go from `dev` to `master`).

Describe the resulting behavior.

## Why

Explain the problem and link any related issue.

## How verified

- [ ] `python -m unittest discover -s tests`
- [ ] `jqqlib --help`
- [ ] `ruff check .` and `mypy`
- [ ] Distribution build and smoke, when packaging changes
- [ ] Behavior changes have unittest coverage
- [ ] Documentation and CHANGELOG updated where needed
- [ ] No market data or credentials included

Record results and explain any checks that do not apply.

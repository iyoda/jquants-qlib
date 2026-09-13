#!/usr/bin/env bash
# Minimal daily wrapper: incremental fetch -> Qlib conversion -> validation -> audit.
#
# Run it from cron / launchd / systemd with JQUANTS_API_KEY exported (or stored
# in the macOS Keychain, see docs/configuration.md, section
# "API Keyはどこに設定する？"). Extra arguments (e.g. --as-of,
# --max-lookback-business-days) are forwarded to `run-daily` and `audit`.
# The audit sidecar location comes from storage.audit_dir in the config.
#
# The jqqlib CLI is resolved in this order: JQQLIB_PYTHON (an interpreter, run
# as `python -m jqqlib`), the checkout's .venv console script, then `jqqlib`
# on PATH.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

CONFIG="${JQQLIB_CONFIG:-config.yaml}"

if [[ -n "${JQQLIB_PYTHON:-}" ]]; then
  JQQLIB=("$JQQLIB_PYTHON" -m jqqlib)
elif [[ -x "${PROJECT_DIR}/.venv/bin/jqqlib" ]]; then
  JQQLIB=("${PROJECT_DIR}/.venv/bin/jqqlib")
else
  JQQLIB=(jqqlib)
fi

export JQQLIB_RUN_STARTED_AT="${JQQLIB_RUN_STARTED_AT:-$(date -u +%Y-%m-%dT%H:%M:%SZ)}"

"${JQQLIB[@]}" run-daily --config "$CONFIG" "$@"
"${JQQLIB[@]}" validate  --config "$CONFIG"
"${JQQLIB[@]}" audit     --config "$CONFIG" "$@"

#!/usr/bin/env bash
# kb-cron-compile.sh — Cron-safe a2kb compile wrapper with timestamped logging
#
# Usage: kb-cron-compile.sh <project-dir> <profile-name>
# Example: kb-cron-compile.sh /path/to/myproject myproject
#
# Logs to <project-dir>/kb-compile.log with timestamps.
#
# Environment overrides:
#   A2KB_BIN — absolute path to the `a2kb` executable (the older KB_BIN is still read).
#             Defaults to whatever `command -v a2kb` resolves on PATH. Useful when cron has a
#             stripped PATH that does not include ~/.local/bin.

set -euo pipefail

PROJECT_DIR="${1:?Usage: $0 <project-dir> <profile-name>}"
PROFILE="${2:?Usage: $0 <project-dir> <profile-name>}"
LOG="${PROJECT_DIR}/kb-compile.log"
KB="${A2KB_BIN:-${KB_BIN:-$(command -v a2kb 2>/dev/null || echo a2kb)}}"

cd "$PROJECT_DIR"

echo "" >> "$LOG"
echo "=== $(date '+%Y-%m-%d %H:%M:%S') compile start (profile: $PROFILE) ===" >> "$LOG"

# Compile (auto-imports Web Clipper clippings, fast-path when nothing changed).
# --strict makes a run in which any file failed or was quarantined exit 1, so cron
# and monitoring see it; it exits 75 when another compile of the KB holds the compile
# lease (this run was skipped, try again later). Capture the code explicitly: under
# `set -e` a failing command would end the script before the "compile done" line is logged.
EXIT_CODE=0
"$KB" -p "$PROFILE" compile --strict >> "$LOG" 2>&1 || EXIT_CODE=$?

echo "=== $(date '+%Y-%m-%d %H:%M:%S') compile done (exit: $EXIT_CODE) ===" >> "$LOG"
exit $EXIT_CODE

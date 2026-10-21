#!/usr/bin/env bash
# kb-cron-lint.sh — Cron-safe a2kb lint wrapper with timestamped logging
#
# Usage: kb-cron-lint.sh <project-dir> <profile-name>
# Logs to <project-dir>/kb-compile.log (same log as compile).
# Structural lint only (zero tokens). Deep health check is separate.

set -euo pipefail

PROJECT_DIR="${1:?Usage: $0 <project-dir> <profile-name>}"
PROFILE="${2:?Usage: $0 <project-dir> <profile-name>}"
LOG="${PROJECT_DIR}/kb-compile.log"
KB="${A2KB_BIN:-${KB_BIN:-$(command -v a2kb 2>/dev/null || echo a2kb)}}"

cd "$PROJECT_DIR"

echo "" >> "$LOG"
echo "=== $(date '+%Y-%m-%d %H:%M:%S') lint start (profile: $PROFILE) ===" >> "$LOG"

"$KB" -p "$PROFILE" lint >> "$LOG" 2>&1
EXIT_CODE=$?

echo "=== $(date '+%Y-%m-%d %H:%M:%S') lint done (exit: $EXIT_CODE) ===" >> "$LOG"
exit $EXIT_CODE

#!/usr/bin/env bash
# kb-cron-backup.sh — Daily wiki archive wrapper.
#
# Usage: kb-cron-backup.sh <project-dir> <profile-name>

set -euo pipefail

PROJECT_DIR="${1:?Usage: $0 <project-dir> <profile-name>}"
PROFILE="${2:?Usage: $0 <project-dir> <profile-name>}"
LOG="${PROJECT_DIR}/kb-backup.log"
KB="${A2KB_BIN:-${KB_BIN:-$(command -v a2kb 2>/dev/null || echo a2kb)}}"

cd "$PROJECT_DIR"

echo "" >> "$LOG"
echo "=== $(date '+%Y-%m-%d %H:%M:%S') backup start (profile: $PROFILE) ===" >> "$LOG"

"$KB" -p "$PROFILE" backup >> "$LOG" 2>&1
EXIT_CODE=$?

echo "=== $(date '+%Y-%m-%d %H:%M:%S') backup done (exit: $EXIT_CODE) ===" >> "$LOG"
exit $EXIT_CODE

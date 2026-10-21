#!/usr/bin/env bash
# kb-cron-stubs.sh — Cron-safe wrapper for `a2kb stubs` with timestamped logging.
#
# Usage: kb-cron-stubs.sh <project-dir> <profile-name>
# Logs to <project-dir>/kb-compile.log (shared log).
#
# Materialises ghost wikilinks (≥5 referrers by default) into stub
# concept pages so readers stop landing on Obsidian's "Create new page"
# prompt when they click an unresolved link. Idempotent — re-running
# refreshes existing stubs' referrer lists, never clobbers real pages.

set -euo pipefail

PROJECT_DIR="${1:?Usage: $0 <project-dir> <profile-name>}"
PROFILE="${2:?Usage: $0 <project-dir> <profile-name>}"
LOG="${PROJECT_DIR}/kb-compile.log"
KB="${A2KB_BIN:-${KB_BIN:-$(command -v a2kb 2>/dev/null || echo a2kb)}}"

cd "$PROJECT_DIR"

echo "" >> "$LOG"
echo "=== $(date '+%Y-%m-%d %H:%M:%S') stubs start (profile: $PROFILE) ===" >> "$LOG"

"$KB" -p "$PROFILE" stubs >> "$LOG" 2>&1
EXIT_CODE=$?

echo "=== $(date '+%Y-%m-%d %H:%M:%S') stubs done (exit: $EXIT_CODE) ===" >> "$LOG"
exit $EXIT_CODE

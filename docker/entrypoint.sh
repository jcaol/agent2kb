#!/usr/bin/env bash
# Container entrypoint.
#   cron (default)  run the crontab with supercronic: the one you mount at /etc/kb/crontab
#                   (or $KB_CRONTAB), else one generated from the KBs in the config
#   anything else   exec as given, e.g. `a2kb bootstrap init`, `a2kb -p alpha status`,
#                   `claude auth login`
set -euo pipefail

if [ "${1:-}" != "cron" ]; then
    exec "$@"
fi

if [ -n "${KB_CRONTAB:-}" ]; then
    CRONTAB="$KB_CRONTAB"
    if [ ! -f "$CRONTAB" ]; then
        echo "KB_CRONTAB=$CRONTAB does not exist." >&2
        exit 1
    fi
elif [ -f /etc/kb/crontab ]; then
    CRONTAB=/etc/kb/crontab
else
    CRONTAB=/home/kb/.kb/crontab.generated
    a2kb bootstrap crontab --out "$CRONTAB"
fi

# The cron wrappers cd into a project directory and write their logs there;
# create every /data/<name> the crontab mentions.
{ grep -v '^[[:space:]]*#' "$CRONTAB" | grep -oE '/data/[A-Za-z0-9._-]+' || true; } \
    | sort -u | while read -r dir; do mkdir -p "$dir"; done

exec supercronic -passthrough-logs "$CRONTAB"

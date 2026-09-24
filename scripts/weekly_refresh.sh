#!/bin/bash
# Weekly refresh: download new data, rebuild everything, publish the reports.
# Run by launchd (see install_weekly_job.sh) or by hand: ./scripts/weekly_refresh.sh
set -uo pipefail
cd "$(dirname "$0")/.."
mkdir -p data/logs
LOG="data/logs/weekly_$(date +%Y%m%d_%H%M).log"
UV="${UV:-$(command -v uv || echo "$HOME/.local/bin/uv")}"
{
  echo "=== weekly refresh $(date) ==="
  # caffeinate keeps the Mac awake for the duration of the run
  /usr/bin/caffeinate -i "$UV" run ere refresh --publish
  echo "=== exit code $? at $(date) ==="
} >> "$LOG" 2>&1

#!/bin/bash
# Install a macOS launchd job that runs scripts/weekly_refresh.sh every Saturday at 09:00.
# If the Mac is asleep then, launchd runs it at the next wake.
#   install:    ./scripts/install_weekly_job.sh
#   uninstall:  launchctl unload ~/Library/LaunchAgents/com.ere.weekly.plist && rm "$_"
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
UV="$(command -v uv || echo "$HOME/.local/bin/uv")"
PLIST="$HOME/Library/LaunchAgents/com.ere.weekly.plist"
chmod +x "$REPO/scripts/weekly_refresh.sh"
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.ere.weekly</string>
  <key>ProgramArguments</key>
  <array><string>/bin/bash</string><string>$REPO/scripts/weekly_refresh.sh</string></array>
  <key>EnvironmentVariables</key>
  <dict><key>UV</key><string>$UV</string>
        <key>PATH</key><string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:$HOME/.local/bin</string></dict>
  <key>StartCalendarInterval</key>
  <dict><key>Weekday</key><integer>6</integer><key>Hour</key><integer>9</integer>
        <key>Minute</key><integer>0</integer></dict>
  <key>StandardOutPath</key><string>$REPO/data/logs/launchd.out</string>
  <key>StandardErrorPath</key><string>$REPO/data/logs/launchd.err</string>
</dict>
</plist>
PL
mkdir -p "$REPO/data/logs"
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "Installed: runs every Saturday 09:00. Logs in $REPO/data/logs/"
echo "Run it now to test:  launchctl start com.ere.weekly"

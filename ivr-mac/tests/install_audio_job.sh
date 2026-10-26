#!/bin/bash
# Install the launchd job run_suite.py uses for the audio layer: it must run
# in the desktop session (audio capture from ssh is silent on macOS).
# Not KeepAlive / RunAtLoad — it only runs when run_suite.py kickstarts it.
set -e
RIG="$(cd "$(dirname "$0")/.." && pwd)"
PL="$HOME/Library/LaunchAgents/com.tyrel.ivr-suite-audio.plist"
mkdir -p "$RIG/tests/results"
cat > "$PL" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.tyrel.ivr-suite-audio</string>
  <key>ProgramArguments</key>
  <array>
    <string>$RIG/.venv/bin/python</string>
    <string>$RIG/tests/audio_replay.py</string>
  </array>
  <key>WorkingDirectory</key><string>$RIG</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>IVR_BASE</key><string>http://127.0.0.1:8130</string>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>PYTHONUNBUFFERED</key><string>1</string>
  </dict>
  <key>StandardOutPath</key><string>$RIG/tests/results/audio_replay.log</string>
  <key>StandardErrorPath</key><string>$RIG/tests/results/audio_replay.log</string>
</dict>
</plist>
EOF
launchctl bootout "gui/$(id -u)/com.tyrel.ivr-suite-audio" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PL"
echo "installed $PL"

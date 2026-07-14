#!/usr/bin/env bash
# Install / uninstall MeetingBar (the menu-bar auto-start app).
#
#   ./install.sh              build + (re)install + start
#   ./install.sh --uninstall  stop + remove agent (keeps source files)
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LABEL="io.meetingbar.autostart"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
UID_NUM="$(id -u)"
STATE="$HOME/.meeting-autostart"

uninstall() {
  echo "Stopping and removing $LABEL ..."
  launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
  pkill -x meetingbar 2>/dev/null || true
  rm -f "$PLIST"
  echo "Removed. (Source files in $DIR kept.)"
}

if [[ "${1:-}" == "--uninstall" ]]; then
  uninstall
  exit 0
fi

# 1. Build the app
echo "Building meetingbar ..."
( cd "$DIR" && swiftc -O MeetingBar.swift -o meetingbar )
chmod +x "$DIR/meetingbar"

# 2. Kill any stray instance (e.g. a manual test run) to avoid two menu-bar icons
pkill -f "$DIR/meetingbar" 2>/dev/null || true

# 3. Write the LaunchAgent plist
mkdir -p "$HOME/Library/LaunchAgents" "$STATE"
cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>            <string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$DIR/meetingbar</string>
  </array>
  <key>RunAtLoad</key>        <true/>
  <key>KeepAlive</key>        <true/>
  <key>ProcessType</key>      <string>Interactive</string>
  <key>StandardOutPath</key>  <string>$STATE/launchd.out.log</string>
  <key>StandardErrorPath</key><string>$STATE/launchd.err.log</string>
</dict>
</plist>
PLIST_EOF

# 4. (Re)load
echo "Loading agent ..."
launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID_NUM" "$PLIST"
launchctl enable "gui/$UID_NUM/$LABEL"
launchctl kickstart -k "gui/$UID_NUM/$LABEL"

echo
echo "✅ Installed. Look for the 🎙 icon in the menu bar."
echo
echo "FIRST RECORDING grants microphone access: when you first hit 'Aufnehmen',"
echo "macOS prompts to let the recorder use the mic. Approve once."
echo
echo "Logs:    $STATE/launchd.err.log  (app)   /  $STATE/recording.log  (recorder)"
echo "Config:  $DIR/config.json"
echo "Remove:  $DIR/install.sh --uninstall"

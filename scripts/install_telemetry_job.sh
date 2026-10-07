#!/usr/bin/env bash
# Installs a macOS launchd job that runs scripts/track_tokens.py once a day
# (US-5.1), so telemetry.csv stays current without a manual step. The job runs
# after the day's sessions have ended, so their transcripts are complete; if the
# Mac is asleep at that time, launchd runs it on wake.
#
#   install_telemetry_job.sh [HH:MM]   install or update the job (default 23:30)
#   install_telemetry_job.sh --print [HH:MM]   print the generated plist only
#   install_telemetry_job.sh --uninstall       stop and remove the job
#
# API keys come from the login Keychain (see track_tokens.py). Output and
# errors are appended to ~/Library/Logs/personal-pilot-timesheet-telemetry.log.
# Python is started directly: a /bin/sh wrapper cannot read its working
# directory under macOS privacy rules for ~/Documents and logs getcwd errors.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="local.personal-pilot-timesheet.telemetry"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/personal-pilot-timesheet-telemetry.log"
DOMAIN="gui/$(id -u)"

usage() {
    echo "Usage: $0 [HH:MM] | --print [HH:MM] | --uninstall" >&2
    exit 2
}

xml_escape() {
    local s="$1"
    s="${s//&/&amp;}"
    s="${s//</&lt;}"
    s="${s//>/&gt;}"
    printf '%s' "$s"
}

plist() {
    local hour="$1" minute="$2"
    cat <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$(xml_escape "$ROOT/.venv/bin/python")</string>
        <string>$(xml_escape "$ROOT/scripts/track_tokens.py")</string>
        <string>-v</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$(xml_escape "$ROOT")</string>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>$hour</integer>
        <key>Minute</key>
        <integer>$minute</integer>
    </dict>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>
    <key>StandardOutPath</key>
    <string>$(xml_escape "$LOG")</string>
    <key>StandardErrorPath</key>
    <string>$(xml_escape "$LOG")</string>
</dict>
</plist>
EOF
}

# Parse HH:MM into "hour minute" without leading zeros (plist integers).
parse_time() {
    local time="${1:-23:30}"
    [[ "$time" =~ ^([01]?[0-9]|2[0-3]):([0-5][0-9])$ ]] || usage
    echo "$((10#${BASH_REMATCH[1]})) $((10#${BASH_REMATCH[2]}))"
}

case "${1:-}" in
    --uninstall)
        [ $# -eq 1 ] || usage
        launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
        rm -f "$PLIST"
        echo "Removed $LABEL"
        ;;
    --print)
        [ $# -le 2 ] || usage
        when="$(parse_time "${2:-}")"  # exits on a bad time
        read -r hour minute <<<"$when"
        plist "$hour" "$minute"
        ;;
    *)
        [ $# -le 1 ] || usage
        when="$(parse_time "${1:-}")"
        read -r hour minute <<<"$when"
        [ -x "$ROOT/.venv/bin/python" ] || { echo "Error: $ROOT/.venv is missing; see CLAUDE.md setup." >&2; exit 1; }
        mkdir -p "$(dirname "$PLIST")" "$(dirname "$LOG")"
        plist "$hour" "$minute" >"$PLIST.tmp"
        plutil -lint -s "$PLIST.tmp" || { rm -f "$PLIST.tmp"; exit 1; }
        mv "$PLIST.tmp" "$PLIST"
        launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true  # reload if already installed
        launchctl bootstrap "$DOMAIN" "$PLIST"
        printf 'Installed %s: daily at %02d:%02d, log %s\n' "$LABEL" "$hour" "$minute" "$LOG"
        ;;
esac

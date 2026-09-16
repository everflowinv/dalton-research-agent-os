#!/bin/zsh
# Point every Dalton LaunchAgent at one release, then restart them all.
# Discovers workspace agents by pattern, so a workspace created yesterday
# is not left behind on an old release (that is exactly what happened to
# ws-7d89 on 2026-09-16: its control service crashed at startup on the
# stale release and the cockpit could not compose a goal).
#
# Usage: scripts/point_services_at_release.sh <release-dir>
#   <release-dir> is the path under ~/.dalton/runtime/releases/<sha>/venv
set -euo pipefail

NEW_VENV="${1:?usage: point_services_at_release.sh <release>/venv}"
[[ "$NEW_VENV" == */venv ]] || { echo "expected a <release>/venv path" >&2; exit 2 }
[[ -x "$NEW_VENV/bin/python" ]] || { echo "no python in $NEW_VENV" >&2; exit 2 }

AGENT_GLOB="$HOME/Library/LaunchAgents/space.lumos.dalton*.plist"
AGENTS=( ${~AGENT_GLOB} )
(( ${#AGENTS[@]} > 0 )) || { echo "no Dalton LaunchAgents found" >&2; exit 2 }

labels=()
for plist in "${AGENTS[@]}"; do
  label=$(basename "$plist" .plist)
  labels+=("$label")
  cur=$(/usr/libexec/PlistBuddy -c "Print :ProgramArguments:0" "$plist" 2>/dev/null || true)
  if [[ "$cur" != "$NEW_VENV/bin/python" ]]; then
    /usr/libexec/PlistBuddy -c "Set :ProgramArguments:0 $NEW_VENV/bin/python" "$plist" >/dev/null
  fi
  # Verify the write landed; a silent mismatch once left every service on
  # the old release for a full patrol.
  got=$(/usr/libexec/PlistBuddy -c "Print :ProgramArguments:0" "$plist" 2>/dev/null)
  if [[ "$got" != "$NEW_VENV/bin/python" ]]; then
    echo "FAILED to repoint $label (still $got)" >&2; exit 3
  fi
  echo "repointed $label"
done

for label in "${labels[@]}"; do
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
done
sleep 3
for label in "${labels[@]}"; do
  launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/$label.plist" 2>/dev/null \
    || { sleep 4; launchctl bootstrap "gui/$(id -u)/$HOME/Library/LaunchAgents/$label.plist" 2>/dev/null || true; }
  launchctl enable "gui/$(id -u)/$label" 2>/dev/null || true
done

echo "restarted ${#labels[@]} services; verifying..."
sleep 15
for label in "${labels[@]}"; do
  pid=$(launchctl print "gui/$(id -u)/$label" 2>/dev/null | awk '/pid = /{print $3; exit}')
  echo "$label pid=${pid:-none}"
done

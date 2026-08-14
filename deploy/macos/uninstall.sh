#!/bin/zsh
set -euo pipefail

domain="gui/$(id -u)"
launch_agents_dir="$HOME/Library/LaunchAgents"

for label in space.lumos.dalton.controller space.lumos.dalton.writer; do
  if launchctl print "$domain/$label" >/dev/null 2>&1; then
    launchctl bootout "$domain/$label"
  fi
  launchctl disable "$domain/$label"
done

for plist in \
  "$launch_agents_dir/space.lumos.dalton.controller.plist" \
  "$launch_agents_dir/space.lumos.dalton.writer.plist"; do
  if [[ -f "$plist" ]]; then
    mv "$plist" "$HOME/.Trash/"
  fi
done

print "Dalton LaunchAgents stopped. Runtime and authority data were preserved."

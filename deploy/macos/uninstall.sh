#!/bin/zsh
set -euo pipefail

domain="gui/$(id -u)"
launch_agents_dir="$HOME/Library/LaunchAgents"

for label in space.lumos.dalton.control space.lumos.dalton.controller space.lumos.dalton.writer; do
  if launchctl print "$domain/$label" >/dev/null 2>&1; then
    launchctl bootout "$domain/$label"
  fi
  launchctl disable "$domain/$label"
done

for plist in \
  "$launch_agents_dir/space.lumos.dalton.control.plist" \
  "$launch_agents_dir/space.lumos.dalton.controller.plist" \
  "$launch_agents_dir/space.lumos.dalton.writer.plist"; do
  if [[ -f "$plist" ]]; then
    mv "$plist" "$HOME/.Trash/"
  fi
done

config_path="$HOME/Library/Application Support/Dalton/config/service.json"
if [[ -f "$config_path" ]] && [[ "$(jq -r '.control.enabled // false' "$config_path")" == "true" ]]; then
  tailscale_source=$(jq -r '.control.config.tailscale_executable' "$config_path")
  control_port=$(jq -r '.control.config.port' "$config_path")
  if [[ -x "$tailscale_source" ]]; then
    "$tailscale_source" serve --https="$control_port" off
  fi
fi

print "Dalton LaunchAgents stopped. Runtime and authority data were preserved."

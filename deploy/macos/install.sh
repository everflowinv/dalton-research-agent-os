#!/bin/zsh
set -euo pipefail

script_dir=${0:A:h}
repo_root=${script_dir:h:h}
dalton_root="$HOME/Library/Application Support/Dalton"
state_dir="$dalton_root/state/dalton-core"
config_dir="$dalton_root/config"
config_path="$config_dir/service.json"
runtime_dir="$dalton_root/runtime"
venv_dir="$runtime_dir/venv"
log_dir="$HOME/Library/Logs/Dalton"
launch_agents_dir="$HOME/Library/LaunchAgents"
python_source=${PYTHON_SOURCE:-/opt/homebrew/bin/python3}
domain="gui/$(id -u)"

mkdir -p "$config_dir" "$runtime_dir" "$log_dir" "$launch_agents_dir"
chmod 700 "$dalton_root" "$config_dir" "$runtime_dir" "$log_dir"

if [[ ! -x "$python_source" ]]; then
  print -u2 "Python 3.11+ not found at $python_source; set PYTHON_SOURCE to an absolute executable."
  exit 2
fi

if [[ ! -x "$venv_dir/bin/python" ]]; then
  "$python_source" -m venv "$venv_dir"
fi
"$venv_dir/bin/python" -m pip install --disable-pip-version-check --upgrade pip
"$venv_dir/bin/python" -m pip install --disable-pip-version-check "${repo_root}[deploy]"

for label in space.lumos.dalton.thesis-impact space.lumos.dalton.control space.lumos.dalton.controller space.lumos.dalton.writer; do
  if launchctl print "$domain/$label" >/dev/null 2>&1; then
    launchctl bootout "$domain/$label"
  fi
done

"$venv_dir/bin/dalton-bootstrap" --state-dir "$state_dir" --config "$config_path"
"$venv_dir/bin/python" -m dalton_core.macos_launchagent \
  --launch-agents-dir "$launch_agents_dir" \
  --python-env-bin "$venv_dir/bin" \
  --state-dir "$state_dir" \
  --config "$config_path" \
  --log-dir "$log_dir"

for label in space.lumos.dalton.writer space.lumos.dalton.controller space.lumos.dalton.control space.lumos.dalton.thesis-impact; do
  plist="$launch_agents_dir/$label.plist"
  if [[ -f "$plist" ]]; then
    launchctl bootstrap "$domain" "$plist"
    launchctl enable "$domain/$label"
    launchctl kickstart -k "$domain/$label"
  fi
done

control_enabled=$(jq -r '.control.enabled // false' "$config_path")
if [[ "$control_enabled" == "true" ]]; then
  tailscale_source=$(jq -r '.control.config.tailscale_executable' "$config_path")
  control_host=$(jq -r '.control.config.host' "$config_path")
  control_port=$(jq -r '.control.config.port' "$config_path")
  if [[ ! -x "$tailscale_source" ]]; then
    print -u2 "Tailscale executable is unavailable: $tailscale_source"
    exit 2
  fi
  "$tailscale_source" serve --bg --yes --https="$control_port" "http://$control_host:$control_port"
fi

for attempt in {1..15}; do
  if "$venv_dir/bin/dalton-health" --config "$config_path" --max-age-seconds 45; then
    exit 0
  fi
  sleep 2
done

"$venv_dir/bin/dalton-health" --config "$config_path" --max-age-seconds 45

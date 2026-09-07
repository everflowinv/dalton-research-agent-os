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
"$venv_dir/bin/python" -m pip install --disable-pip-version-check "${repo_root}[deploy,pdf]"

# P9d-11: stopping the writer terminates whatever lane child is in flight and
# the next tick settles it as orphaned, parking that company/spec for a day.
# The controller is what launches a child every tick, so it goes down first;
# then wait (bounded) for running children; then stop the writer.  On timeout
# say so and proceed.  The drain is read-only: it never signals a child or
# touches Core.  (First deploy with the drain after the controller waited the
# full timeout because the controller kept launching children behind it.)
for label in space.lumos.dalton.thesis-impact space.lumos.dalton.control space.lumos.dalton.controller; do
  if launchctl print "$domain/$label" >/dev/null 2>&1; then
    launchctl bootout "$domain/$label"
  fi
done
if ! "$venv_dir/bin/python" -m dalton_core.launch_drain --state-dir "$state_dir" --timeout "${DRAIN_TIMEOUT:-600}"; then
  print -u2 "warning: lane children still running after drain timeout; proceeding with bootout"
fi
if launchctl print "$domain/space.lumos.dalton.writer" >/dev/null 2>&1; then
  launchctl bootout "$domain/space.lumos.dalton.writer"
fi

"$venv_dir/bin/dalton-bootstrap" --state-dir "$state_dir" --config "$config_path"

# Connector governance: the writer launches AlphaEngine acquisitions only
# against an *approved* record at this path.  Seed the committed proposal once;
# never overwrite an existing (possibly approved) record.  The owner approves
# in place with: dalton-connector-governance approve --path <file> --approved-by human:<owner>
governance_dir="$state_dir/connector-governance"
governance_file="$governance_dir/alphaengine-get-document-v1.json"
mkdir -p "$governance_dir"
chmod 700 "$governance_dir"
if [[ ! -f "$governance_file" ]]; then
  cp "$repo_root/deploy/connector-governance/alphaengine-get-document-v1.json" "$governance_file"
  chmod 600 "$governance_file"
fi
# S7d: the SEC company-facts lane has its own record; same seed-once rule.
sec_governance_file="$governance_dir/sec-company-facts-v1.json"
if [[ ! -f "$sec_governance_file" && -f "$repo_root/deploy/connector-governance/sec-company-facts-v1.json" ]]; then
  cp "$repo_root/deploy/connector-governance/sec-company-facts-v1.json" "$sec_governance_file"
  chmod 600 "$sec_governance_file"
fi
# P9b-1: the company-facts template hash moved; the writer now launches the
# lane against the v2 record.  Seed once as *proposed*; the owner approves in
# place with dalton-connector-governance approve.
sec_governance_v2_file="$governance_dir/sec-company-facts-v2.json"
if [[ ! -f "$sec_governance_v2_file" && -f "$repo_root/deploy/connector-governance/sec-company-facts-v2.json" ]]; then
  cp "$repo_root/deploy/connector-governance/sec-company-facts-v2.json" "$sec_governance_v2_file"
  chmod 600 "$sec_governance_v2_file"
fi
# P9d-1: AlphaEngine search_library is a separate governed capability.  Seed
# the committed *proposed* record once; the owner approves in place with
# dalton-connector-governance approve.  The discovery plan is a hash-bound
# manifest the writer loads at start; seed once, never overwrite.
search_governance_file="$governance_dir/alphaengine-search-library-v1.json"
if [[ ! -f "$search_governance_file" && -f "$repo_root/deploy/connector-governance/alphaengine-search-library-v1.json" ]]; then
  cp "$repo_root/deploy/connector-governance/alphaengine-search-library-v1.json" "$search_governance_file"
  chmod 600 "$search_governance_file"
fi
plan_dir="$state_dir/discovery-plans"
plan_file="$plan_dir/us-it-services-alphaengine-v1.json"
mkdir -p "$plan_dir"
chmod 700 "$plan_dir"
if [[ ! -f "$plan_file" && -f "$repo_root/deploy/phase9/p9d-us-it-services-discovery-plan-v1.json" ]]; then
  cp "$repo_root/deploy/phase9/p9d-us-it-services-discovery-plan-v1.json" "$plan_file"
  chmod 600 "$plan_file"
fi
# P9d-4a: Gemini web search is its own governed capability with its own
# 0.2 discovery plan.  Seed both once as *proposed* / hash-bound; the owner
# approves the record in place with dalton-connector-governance approve.
# Seeding creates no authority: the live mission still marks
# source:web-search as not_connected, and this slice has no live transport.
web_search_governance_file="$governance_dir/gemini-web-search-v1.json"
if [[ ! -f "$web_search_governance_file" && -f "$repo_root/deploy/connector-governance/gemini-web-search-v1.json" ]]; then
  cp "$repo_root/deploy/connector-governance/gemini-web-search-v1.json" "$web_search_governance_file"
  chmod 600 "$web_search_governance_file"
fi
# P9d-4b: public-web fetch of URLs a web search cited; same seed-once rule.
web_fetch_governance_file="$governance_dir/web-fetch-v1.json"
if [[ ! -f "$web_fetch_governance_file" && -f "$repo_root/deploy/connector-governance/web-fetch-v1.json" ]]; then
  cp "$repo_root/deploy/connector-governance/web-fetch-v1.json" "$web_fetch_governance_file"
  chmod 600 "$web_fetch_governance_file"
fi
# P9d-8/13: the plan is hash bound, so raising its 24h call budget (v2) or
# adding an acquisition policy (v3: preferred / skipped hosts) is a new plan
# version rather than an edit in place.  Seed once, same rule as v1.
web_plan_file="$plan_dir/us-it-services-web-search-v3.json"
if [[ ! -f "$web_plan_file" && -f "$repo_root/deploy/phase9/p9d4-us-it-services-web-search-plan-v3.json" ]]; then
  cp "$repo_root/deploy/phase9/p9d4-us-it-services-web-search-plan-v3.json" "$web_plan_file"
  chmod 600 "$web_plan_file"
fi
# ADR-0005 / P9d-17a: the writer needs an approved extraction model
# configuration for drafting to run as mission automation.  Idempotent: appends
# the extraction routing policy only if its filters changed, writes the closed
# config next to the state, and points service.json at it.  No credential is
# read; the broker key path is referenced.
"$venv_dir/bin/python" -m dalton_core.document_extraction_setup --config "$config_path"
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

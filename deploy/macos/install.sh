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
# P13z: the company-facts output contract now admits a filing whose calendar
# frame passed to a later one -- SEC moves the frame to the newest filing that
# reports a period, so the prior-year quarter a later 10-Q repeats has none,
# and refusing those made every historical filing unusable. That moves the
# schema hash, so the v2 approval no longer covers it. Same seed-once rule:
# copied in as *proposed*, and the owner approves in place with
# `dalton-connector-governance approve`. Until then the lane fails closed.
sec_governance_v3_file="$governance_dir/sec-company-facts-v3.json"
if [[ ! -f "$sec_governance_v3_file" && -f "$repo_root/deploy/connector-governance/sec-company-facts-v3.json" ]]; then
  cp "$repo_root/deploy/connector-governance/sec-company-facts-v3.json" "$sec_governance_v3_file"
  chmod 600 "$sec_governance_v3_file"
fi
# P13ae: Guidepoint is the expert-network library the Deep Insight Gate asks
# for -- filings say what a company reported, operators say why. Two records,
# because reading the index and reading a transcript are different permissions
# and a schema hash binds one operation. Same seed-once rule: copied in as
# *proposed*, and the owner approves each in place with
# `dalton-connector-governance approve`. Until then the lane cannot run.
for guidepoint_kind in guidepoint-search-library guidepoint-get-transcript; do
  guidepoint_file="$governance_dir/${guidepoint_kind}-v1.json"
  if [[ ! -f "$guidepoint_file" && -f "$repo_root/deploy/connector-governance/${guidepoint_kind}-v1.json" ]]; then
    cp "$repo_root/deploy/connector-governance/${guidepoint_kind}-v1.json" "$guidepoint_file"
    chmod 600 "$guidepoint_file"
  fi
done
# P13ag: SEC financial statements read through edgartools -- the same SEC as
# the filings lane, read as statements rather than one XBRL concept at a time,
# so a model can have line items at all. It does not replace reading filings:
# whatever the parser cannot reach still comes from the original text.
# Credential-free public HTTPS to two named SEC hosts. Seed once as *proposed*.
sec_financials_file="$governance_dir/sec-financial-statements-v1.json"
if [[ ! -f "$sec_financials_file" && -f "$repo_root/deploy/connector-governance/sec-financial-statements-v1.json" ]]; then
  cp "$repo_root/deploy/connector-governance/sec-financial-statements-v1.json" "$sec_financials_file"
  chmod 600 "$sec_financials_file"
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
# P10u: the SEC filings index asks for a form per issuer rather than a phrase,
# so it is a 0.4 plan. Seeded once and hash bound like the others; the approval
# it runs under is the one the owner already signed.
sec_plan_file="$plan_dir/us-it-services-sec-filings-v1.json"
if [[ ! -f "$sec_plan_file" && -f "$repo_root/deploy/phase10/p10-us-it-services-sec-filings-plan-v1.json" ]]; then
  cp "$repo_root/deploy/phase10/p10-us-it-services-sec-filings-plan-v1.json" "$sec_plan_file"
  chmod 600 "$sec_plan_file"
fi
# ADR-0005 / P9d-17a: the writer needs an approved extraction model
# configuration for drafting to run as mission automation.  Idempotent: appends
# the extraction routing policy only if its filters changed, writes the closed
# config next to the state, and points service.json at it.  No credential is
# read; the broker key path is referenced.
"$venv_dir/bin/python" -m dalton_core.document_extraction_setup --config "$config_path"
# P13k: the planner's model, only when the owner names one. It decides what the
# research works on next, so it routes through its own policy rather than
# sharing extraction's -- which pins a single profile by design. Left unset
# nothing is installed and the planner stays dark: its model costs fifty times
# the extraction model's, which is affordable for a few small calls a day and
# is not something anyone should acquire by upgrading.
#
#   DALTON_PLANNER_MODEL_PROFILE=profile:gpt-6-astra
if [[ -n "${DALTON_PLANNER_MODEL_PROFILE:-}" ]]; then
  "$venv_dir/bin/python" -m dalton_core.research_planner_setup \
    --config "$config_path" --profile-ids "$DALTON_PLANNER_MODEL_PROFILE"
fi
# P13ad: the deliverable is written, not extracted. Until this was set the
# Initial Screen was drafted by the extraction model -- the one chosen to pull a
# figure out of one window of a filing, cheaply, thousands of times. The
# sections that carry the argument (core thesis, risks and anti-thesis,
# read-across to the universe) are exactly where that shows: live, the
# anti-thesis section came back dropped_unsourced and the relevance section
# published with no figures at all.
#
# Unset and nothing changes: the screen keeps using the extraction model. A
# screen costs about $0.013 on the extraction model and roughly $0.6-1.0 on a
# frontier one, so this is a real but small standing cost, and it is the
# owner's to choose rather than to inherit.
#
#   DALTON_DELIVERABLE_MODEL_PROFILE=profile:gpt-6-astra
if [[ -n "${DALTON_DELIVERABLE_MODEL_PROFILE:-}" ]]; then
  "$venv_dir/bin/python" -m dalton_core.deliverable_model_setup \
    --config "$config_path" --profile-ids "$DALTON_DELIVERABLE_MODEL_PROFILE"
fi
# P9d-18 / ADR-0006: point the cockpit at the Core (read-only), the state
# directory, the heartbeat, the scheduler and the extraction model config so
# the owner's page can show progress, answer questions and draft goals.
"$venv_dir/bin/python" -m dalton_core.cockpit_setup --config "$config_path"
# P10f/P10h: DALTON_EXTRACTION_MAX_WINDOWS raises reading throughput. Each
# window is one paid model call against the mission's max_daily_paid_calls, so
# raise the mission budget first.
#
# Setting it persists it into service.json, so a later plain re-install keeps
# the owner's number instead of silently restoring the built-in default -- the
# first install after this knob existed did exactly that and put reading back
# to 4 without saying so.
if [[ -n "${DALTON_ALPHAENGINE_OWNER_CALL_CAP:-}" ]]; then
  "$venv_dir/bin/python" - "$config_path" "$DALTON_ALPHAENGINE_OWNER_CALL_CAP" <<'PYCAP'
import json, sys
from pathlib import Path

path, cap = Path(sys.argv[1]), sys.argv[2]
if not cap.isdigit() or not 1 <= int(cap) <= 2000:
    raise SystemExit("DALTON_ALPHAENGINE_OWNER_CALL_CAP must be an integer 1..2000")
config = json.loads(path.read_text(encoding="utf-8"))
config["alphaengine_owner_call_cap"] = int(cap)
path.write_text(json.dumps(config, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print("alphaengine_owner_call_cap=" + cap)
PYCAP
fi
if [[ -n "${DALTON_EXTRACTION_MAX_WINDOWS:-}" || -n "${DALTON_EXTRACTION_NUMERIC_WINDOWS:-}" \
   || -n "${DALTON_EXTRACTION_DISCOVERY_WINDOWS:-}" ]]; then
  "$venv_dir/bin/python" - "$config_path" "${DALTON_EXTRACTION_MAX_WINDOWS:-}" \
    "${DALTON_EXTRACTION_NUMERIC_WINDOWS:-}" "${DALTON_EXTRACTION_DISCOVERY_WINDOWS:-}" <<'PYSETUP'
import json, sys
from pathlib import Path

path, prose, numeric, discovery = Path(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]
config = json.loads(path.read_text(encoding="utf-8"))
block = dict(config.get("document_extraction") or {})
if prose:
    if not prose.isdigit() or not 1 <= int(prose) <= 50:
        raise SystemExit("DALTON_EXTRACTION_MAX_WINDOWS must be an integer 1..50")
    block["max_windows_per_tick"] = int(prose)
if numeric:
    if not numeric.isdigit() or not 0 <= int(numeric) <= 50:
        raise SystemExit("DALTON_EXTRACTION_NUMERIC_WINDOWS must be an integer 0..50")
    block["numeric_windows_per_tick"] = int(numeric)
if discovery:
    if not discovery.isdigit() or not 0 <= int(discovery) <= 50:
        raise SystemExit("DALTON_EXTRACTION_DISCOVERY_WINDOWS must be an integer 0..50")
    block["discovery_windows_per_tick"] = int(discovery)
if "max_windows_per_tick" not in block:
    raise SystemExit("set DALTON_EXTRACTION_MAX_WINDOWS before the secondary passes")
config["document_extraction"] = block
path.write_text(json.dumps(config, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print("document_extraction=" + json.dumps(block, sort_keys=True))
PYSETUP
fi
# An array, not ${VAR:+...}: this script runs under zsh, which does not word
# split an unquoted expansion, so the flag and its value would arrive as one
# argument and argparse would refuse the install.
extraction_window_args=()
if [[ -n "${DALTON_EXTRACTION_MAX_WINDOWS:-}" ]]; then
  extraction_window_args=(--extraction-max-windows "$DALTON_EXTRACTION_MAX_WINDOWS")
fi
"$venv_dir/bin/python" -m dalton_core.macos_launchagent \
  --launch-agents-dir "$launch_agents_dir" \
  --python-env-bin "$venv_dir/bin" \
  --state-dir "$state_dir" \
  --config "$config_path" \
  --log-dir "$log_dir" \
  "${extraction_window_args[@]}"

# P10h: ``launchctl bootout`` returns before the job is gone.  Bootstrapping
# into a domain that still holds the old job fails with "Input/output error",
# and under ``set -e`` that aborted the install *after* the bootout -- leaving
# every service down.  Seen live: a second install run seconds after the first
# took the whole system offline and said only "Bootstrap failed: 5".
# So wait for the unload, and treat an already-loaded job as success.
for label in space.lumos.dalton.writer space.lumos.dalton.controller space.lumos.dalton.control space.lumos.dalton.thesis-impact; do
  plist="$launch_agents_dir/$label.plist"
  if [[ -f "$plist" ]]; then
    if launchctl print "$domain/$label" >/dev/null 2>&1; then
      launchctl bootout "$domain/$label" 2>/dev/null || true
    fi
    for _ in {1..50}; do
      launchctl print "$domain/$label" >/dev/null 2>&1 || break
      sleep 0.2
    done
    if ! launchctl bootstrap "$domain" "$plist" 2>/dev/null; then
      # Only a job that is genuinely absent is a failure worth stopping for.
      if ! launchctl print "$domain/$label" >/dev/null 2>&1; then
        print -u2 "error: $label did not bootstrap and is not loaded"
        exit 1
      fi
      print -u2 "note: $label was already loaded; kickstarting it instead"
    fi
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

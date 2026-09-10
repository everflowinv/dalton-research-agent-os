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
"$venv_dir/bin/python" -m pip install --disable-pip-version-check "${repo_root}[deploy,pdf,sec-financials,market-data]"

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

# INT3: the governance records this repository ships and this script
# deliberately does *not* copy into connector-governance/. Every other
# committed record is seeded below as part of its lane's all-or-nothing block,
# and `tests/test_service.py::DeliberatelyUnseededTests` asserts the two sets
# together cover `deploy/connector-governance/` exactly -- so a record added to
# the repo and forgotten here fails the suite rather than becoming a lane that
# reports `unconfigured` for ever with nobody having decided that.
#
# This array is read by nothing at runtime. It exists so the reason lives next
# to the decision instead of in a report, and so the test above has something
# to check against.
DELIBERATELY_UNSEEDED=(
  # P13ah: roic.ai answers 403 on every page, site-wide, from 2026-08-29 --
  # confirmed here (NVDA, AAPL) and independently by the OpenClaw source survey
  # (`docs/reports/openclaw-data-source-survey-v1.0-2026-09-09.md`), which
  # reaches it the same way: bare HTTPS with a browser user agent, no cookie,
  # no key, no proxy. Nothing in the writer loads either record, so seeding
  # them buys no lane and costs the owner two approvals to make about a source
  # that answers nothing. The records stay committed: when roic is reachable
  # again the only change here is to seed them.
  roic-list-transcripts-v1.json
  roic-get-transcript-v1.json
  # S2: the narrowing record is a *note*, not an approval. It describes an
  # operation the upstream does not have, nothing loads it, and a permanently
  # proposed record sitting in the runtime directory is an approval to make
  # about nothing. It is seeded into governance-decisions/ instead (below),
  # where the owner reads it and no lane looks.
  guidepoint-get-transcript-narrowing-v1.json
)

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
# P13ah / INT3: the two roic.ai records are committed and are *not* seeded --
# see DELIBERATELY_UNSEEDED at the top of this file for why.
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
# v2: the v1 contract had no period_start, and a 10-Q reports the quarter and
# the year to date under the same period_end -- EPAM's Q2 and H1 revenue both
# end 2026-06-30. Without the start they are one number twice, and half-years
# would have been ingested as quarters. Caught before any data was taken.
for sec_financials_version in v1 v2; do
  sec_financials_file="$governance_dir/sec-financial-statements-${sec_financials_version}.json"
  repo_record="$repo_root/deploy/connector-governance/sec-financial-statements-${sec_financials_version}.json"
  if [[ ! -f "$sec_financials_file" && -f "$repo_record" ]]; then
    cp "$repo_record" "$sec_financials_file"
    chmod 600 "$sec_financials_file"
  fi
done
# P11a: yfinance daily prices and analyst estimates. Same seed-once rule as
# every connector above: the committed record is copied in as *proposed* and
# the owner approves it in place with `dalton-connector-governance approve`.
# Two records because reading a price series and reading what the street
# thinks are different permissions, and a schema hash binds one operation.
# The daily-prices record is the switch for the whole price lane: without it
# on disk the writer's plist carries no --market-price-governance, the lane
# is not installed, and the Core runs exactly as it did. The estimates record
# has no consumer until Wave 2 and can stay proposed.
for yfinance_kind in yfinance-daily-prices yfinance-analyst-estimates; do
  yfinance_file="$governance_dir/${yfinance_kind}-v1.json"
  if [[ ! -f "$yfinance_file" && -f "$repo_root/deploy/connector-governance/${yfinance_kind}-v1.json" ]]; then
    cp "$repo_root/deploy/connector-governance/${yfinance_kind}-v1.json" "$yfinance_file"
    chmod 600 "$yfinance_file"
  fi
done
# INT2: the rule INT1 wrote down and this block keeps -- a lane is seeded all
# or nothing. Copying half of what a lane needs gives the owner an approval to
# make and a lane that starts and refuses every tick, which reads like a fault
# rather than an absence. So each block below either puts down everything its
# lane switches on, or puts down nothing and says why.
#
# C1: the catalyst calendar reads Yahoo's diary for the covered companies. Its
# own record, because a schema hash binds one operation and an approval to read
# prices is not an approval to read anything else Yahoo serves. This one record
# is the whole switch: without it the writer's plist carries no
# --catalyst-calendar-governance and the lane is not installed. Seeded once as
# *proposed*; the owner approves in place with `dalton-connector-governance
# approve`. The lane needs no new mission version -- the live mission already
# grants `observation`.
calendar_governance_file="$governance_dir/yfinance-calendar-v1.json"
if [[ ! -f "$calendar_governance_file" && -f "$repo_root/deploy/connector-governance/yfinance-calendar-v1.json" ]]; then
  cp "$repo_root/deploy/connector-governance/yfinance-calendar-v1.json" "$calendar_governance_file"
  chmod 600 "$calendar_governance_file"
fi
# S4: the six China / Hong Kong fundamentals records. There is no lane for them
# in this wave -- the mission universe is US-listed -- so seeding them turns
# nothing on and cannot half-turn-on anything. They are here so the owner can
# read and approve them in place; six schema hashes, six separate approvals,
# because approving statements is not approving northbound flow.
for cn_hk_kind in financial-statements shareholders buybacks margin-balance \
                  northbound-flow ah-premium; do
  cn_hk_file="$governance_dir/cn-hk-findata-${cn_hk_kind}-v1.json"
  if [[ ! -f "$cn_hk_file" && -f "$repo_root/deploy/connector-governance/cn-hk-findata-${cn_hk_kind}-v1.json" ]]; then
    cp "$repo_root/deploy/connector-governance/cn-hk-findata-${cn_hk_kind}-v1.json" "$cn_hk_file"
    chmod 600 "$cn_hk_file"
  fi
done
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
#
# INT3: the lane's governance record is seeded here too, because the writer's
# plist names `--sec-filings-governance <state>/connector-governance/
# sec-filings-index-v1.json` *unconditionally*. Until this block existed the
# record lived only on the live Core: it was proposed and approved in place
# there and was never committed, so a Core rebuilt from scratch got a writer
# pointed at a path with no file behind it. The record is recovered from that
# Core byte for byte, and it re-derives from the packaged SEC template through
# `sec_filings_index.build_filings_index_governance_record` -- same source
# hash, same schema hash, same content hash -- so it is the packaged contract's
# own record rather than a copy of somebody's disk.
#
# It is committed and seeded *approved*, which is not the usual rule and is the
# same exception `alphaengine-get-document-v1.json` and `sec-company-facts-v1.
# json` already carry: the approval is the owner's own, made on 2026-08-26,
# and re-proposing it here would take an approval away on the next deploy
# rather than ask for one. Copy-once like everything else, so a live Core's
# record is never overwritten.
sec_filings_governance_file="$governance_dir/sec-filings-index-v1.json"
sec_plan_file="$plan_dir/us-it-services-sec-filings-v1.json"
if [[ -f "$repo_root/deploy/connector-governance/sec-filings-index-v1.json" \
   && -f "$repo_root/deploy/phase10/p10-us-it-services-sec-filings-plan-v1.json" ]]; then
  if [[ ! -f "$sec_filings_governance_file" ]]; then
    cp "$repo_root/deploy/connector-governance/sec-filings-index-v1.json" "$sec_filings_governance_file"
    chmod 600 "$sec_filings_governance_file"
  fi
  if [[ ! -f "$sec_plan_file" ]]; then
    cp "$repo_root/deploy/phase10/p10-us-it-services-sec-filings-plan-v1.json" "$sec_plan_file"
    chmod 600 "$sec_plan_file"
  fi
fi
# S2 / INT2: the Guidepoint expert-network lane. Its two governance records are
# already seeded above (P13ae); what it also wants is a discovery plan, and the
# lane's argv fragment requires *both* files -- the plan being on disk is the
# other half of the switch. Seeded together with the records, so the pair is
# never half present. The narrowing record is deliberately NOT put in
# connector-governance/: nothing loads it, it is the note the owner reads
# before deciding what to do with the approval for an operation the upstream
# does not have, and a permanently-proposed record in the runtime directory is
# an approval to make about nothing.
gp_plan_file="$plan_dir/us-it-services-guidepoint-v1.json"
if [[ ! -f "$gp_plan_file" && -f "$repo_root/deploy/phase9/p9-us-it-services-guidepoint-v1.json" ]]; then
  cp "$repo_root/deploy/phase9/p9-us-it-services-guidepoint-v1.json" "$gp_plan_file"
  chmod 600 "$gp_plan_file"
fi
decisions_dir="$state_dir/governance-decisions"
mkdir -p "$decisions_dir"
chmod 700 "$decisions_dir"
gp_narrowing_file="$decisions_dir/guidepoint-get-transcript-narrowing-v1.json"
if [[ ! -f "$gp_narrowing_file" && -f "$repo_root/deploy/connector-governance/guidepoint-get-transcript-narrowing-v1.json" ]]; then
  cp "$repo_root/deploy/connector-governance/guidepoint-get-transcript-narrowing-v1.json" "$gp_narrowing_file"
  chmod 600 "$gp_narrowing_file"
fi
# S1 / INT2: the two human-feed lanes read files a host skill already wrote to
# this disk. Neither one is a network connector, so what turns them on is the
# feed plan, their approved records, and the source directory *being there*.
# The workspace is named by an environment variable rather than assumed,
# because a Core installed without OpenClaw has no feeds and should end up
# with no feed lane rather than with two lanes that refuse every tick.
#
#   DALTON_OPENCLAW_WORKSPACE=~/.openclaw/workspace   (the default)
#
# The two lanes are seeded independently: sales notes and the company wiki are
# separate approvals, separate directories and separate LaneSpecs, so one
# being absent must not take the other with it.
openclaw_workspace=${DALTON_OPENCLAW_WORKSPACE:-$HOME/.openclaw/workspace}
feed_plan_dir="$state_dir/feed-plans"
feeds_dir="$state_dir/feeds"
digest_source="$openclaw_workspace/skills/market-digest/output"
wiki_index_source="$openclaw_workspace/wiki-index.sqlite"
# W3: the plan is versioned rather than re-copied. Seeding is copy-once by
# design -- the state directory is the owner's, and a script that overwrites
# what is there is a script that can undo a hand edit -- so a plan whose
# *contents* changed has to arrive under a new name or it never lands on a
# Core that already installed the old one. v2 is v1 plus `source:prior-research`;
# the three feed lanes name v2 by constant, so a Core that has only v1 on disk
# brings up no feed lane until install.sh is re-run, which is the one step the
# deploy runbook already has.
seed_feed_plan() {
  mkdir -p "$feed_plan_dir"
  chmod 700 "$feed_plan_dir"
  feed_plan_file="$feed_plan_dir/p9-us-it-services-feeds-v2.json"
  if [[ ! -f "$feed_plan_file" && -f "$repo_root/deploy/phase9/p9-us-it-services-feeds-v2.json" ]]; then
    cp "$repo_root/deploy/phase9/p9-us-it-services-feeds-v2.json" "$feed_plan_file"
    chmod 600 "$feed_plan_file"
  fi
}
if [[ -d "$digest_source" ]]; then
  seed_feed_plan
  for sales_notes_kind in sales-notes-list-notes sales-notes-get-note; do
    sales_notes_file="$governance_dir/${sales_notes_kind}-v1.json"
    if [[ ! -f "$sales_notes_file" && -f "$repo_root/deploy/connector-governance/${sales_notes_kind}-v1.json" ]]; then
      cp "$repo_root/deploy/connector-governance/${sales_notes_kind}-v1.json" "$sales_notes_file"
      chmod 600 "$sales_notes_file"
    fi
  done
  mkdir -p "$feeds_dir"
  chmod 700 "$feeds_dir"
  # A link, not a copy: the digest directory is the skill's own output and
  # gets a new file twice a day. A copy would be a second, stale truth.
  if [[ ! -e "$feeds_dir/market-digest-output" ]]; then
    ln -s "$digest_source" "$feeds_dir/market-digest-output"
  fi
else
  print "note: no market-digest output at $digest_source; the sales-note lane is not installed."
fi
# The wiki corpus is 258 MB and its index rows carry paths relative to the
# workspace root, so the corpus root has to *be* the workspace: a link to a
# subdirectory would make every document path escape the root and be refused.
# The index therefore has to be reachable as <workspace>/wiki-index.sqlite,
# which is the owner's one line to run (see the owner-steps document); this
# script does not write inside the OpenClaw workspace.
if [[ -d "$openclaw_workspace" && -e "$wiki_index_source" ]]; then
  seed_feed_plan
  for company_wiki_kind in company-wiki-list-documents company-wiki-get-document; do
    company_wiki_file="$governance_dir/${company_wiki_kind}-v1.json"
    if [[ ! -f "$company_wiki_file" && -f "$repo_root/deploy/connector-governance/${company_wiki_kind}-v1.json" ]]; then
      cp "$repo_root/deploy/connector-governance/${company_wiki_kind}-v1.json" "$company_wiki_file"
      chmod 600 "$company_wiki_file"
    fi
  done
  mkdir -p "$feeds_dir"
  chmod 700 "$feeds_dir"
  if [[ ! -e "$feeds_dir/company-wiki" ]]; then
    ln -s "$openclaw_workspace" "$feeds_dir/company-wiki"
  fi
else
  print "note: no wiki index at $wiki_index_source; the company-wiki lane is not installed."
fi
# W3: the fund's own earlier work on a company -- old Initial Screens, memos,
# notes, maintained Excel models. Unlike the two S1 feeds above there is no
# workspace to discover: the owner declares where the material is, because
# there is no safe default for "somewhere on this disk there are our old
# files" and guessing at one would either find nothing or find something that
# is not ours.
#
#   DALTON_PRIOR_RESEARCH_DIR=~/Documents/dalton-prior-research
#
# The directory holds one folder per company, each with a manifest.json naming
# its documents and -- the one thing the feed will not guess -- each one's
# date. All of it together or none of it: two records and the corpus link, or
# nothing and a note saying which variable to set. A lane switched on with no
# corpus refuses every tick, which reads like a fault rather than an absence.
prior_research_dir=${DALTON_PRIOR_RESEARCH_DIR:-}
if [[ -n "$prior_research_dir" && -d "$prior_research_dir" ]]; then
  seed_feed_plan
  for prior_research_kind in prior-research-list-documents prior-research-get-document; do
    prior_research_file="$governance_dir/${prior_research_kind}-v1.json"
    if [[ ! -f "$prior_research_file" && -f "$repo_root/deploy/connector-governance/${prior_research_kind}-v1.json" ]]; then
      cp "$repo_root/deploy/connector-governance/${prior_research_kind}-v1.json" "$prior_research_file"
      chmod 600 "$prior_research_file"
    fi
  done
  mkdir -p "$feeds_dir"
  chmod 700 "$feeds_dir"
  # A link, not a copy: the owner keeps adding to this directory and a copy
  # would be a second, stale truth. The manifest paths are relative to each
  # company folder, so the link has to be the declared root itself.
  # `-e` alone is false for a *dangling* symlink, so a link left over from a
  # directory the owner has since moved would be silently re-created beside
  # itself -- or rather not re-created, and the lane would come up pointing at
  # nothing. `-L` catches the dangling case; `ln -sfn` replaces it.
  if [[ ! -e "$feeds_dir/prior-research" && ! -L "$feeds_dir/prior-research" ]]; then
    ln -s "$prior_research_dir" "$feeds_dir/prior-research"
  elif [[ -L "$feeds_dir/prior-research" && ! -e "$feeds_dir/prior-research" ]]; then
    print "note: $feeds_dir/prior-research is a dangling link; repointing it at $prior_research_dir."
    ln -sfn "$prior_research_dir" "$feeds_dir/prior-research"
  fi
else
  print "note: set DALTON_PRIOR_RESEARCH_DIR to an existing directory to install the prior-research lane."
fi
# S3 / INT2: the three crowd sources. Seven records, a per-company map of
# handles and queries, and three host tools this Core does not know the
# location of. All of it together or none of it: the map is the lane's switch,
# and a lane switched on with no tool refuses every networked run with "no
# tool configured", which is the failure this rule exists to prevent.
#
#   DALTON_AGENT_REACH_TOOL=/opt/homebrew/bin/agent-reach   (Xueqiu channel)
#   DALTON_XUEQIU_HOT_RANK_TOOL=...                         (the hot-rank shim)
#   DALTON_XREACH_TOOL=/opt/homebrew/bin/xreach             (X timelines)
#
# Xueqiu's hot rank is not a subcommand of agent-reach, so it needs its own
# small shim; the other two subcommands are agent-reach's own. Even with all
# three present the lane stays off until the owner approves a record: all seven
# ship *proposed* and the lane reads the status rather than the file's presence.
crowd_tools_dir="$state_dir/host-tools"
if [[ -x "${DALTON_AGENT_REACH_TOOL:-}" && -x "${DALTON_XUEQIU_HOT_RANK_TOOL:-}" \
   && -x "${DALTON_XREACH_TOOL:-}" ]]; then
  mkdir -p "$crowd_tools_dir"
  chmod 700 "$crowd_tools_dir"
  ln -sfn "$DALTON_AGENT_REACH_TOOL" "$crowd_tools_dir/agent-reach"
  ln -sfn "$DALTON_XUEQIU_HOT_RANK_TOOL" "$crowd_tools_dir/xueqiu-hot-rank"
  ln -sfn "$DALTON_XREACH_TOOL" "$crowd_tools_dir/xreach"
  for crowd_kind in xueqiu-search-posts xueqiu-get-post xueqiu-hot-rank \
                    x-xreach-user-timeline x-xreach-search x-xreach-thread \
                    employee-reviews-blind; do
    crowd_file="$governance_dir/${crowd_kind}-v1.json"
    if [[ ! -f "$crowd_file" && -f "$repo_root/deploy/connector-governance/${crowd_kind}-v1.json" ]]; then
      cp "$repo_root/deploy/connector-governance/${crowd_kind}-v1.json" "$crowd_file"
      chmod 600 "$crowd_file"
    fi
  done
  phase9_dir="$state_dir/phase9"
  mkdir -p "$phase9_dir"
  chmod 700 "$phase9_dir"
  crowd_map_file="$phase9_dir/p9-us-it-services-crowd-sources-v1.json"
  if [[ ! -f "$crowd_map_file" && -f "$repo_root/deploy/phase9/p9-us-it-services-crowd-sources-v1.json" ]]; then
    cp "$repo_root/deploy/phase9/p9-us-it-services-crowd-sources-v1.json" "$crowd_map_file"
    chmod 600 "$crowd_map_file"
  fi
else
  print "note: set DALTON_AGENT_REACH_TOOL, DALTON_XUEQIU_HOT_RANK_TOOL and DALTON_XREACH_TOOL to install the crowd-source lane."
fi
# P14a / INT2: the tracking policy is the daily-tracking lane's whole switch --
# a tracking lane with no baselines has no opinion about how often to look at
# anything. One file, so all-or-nothing is automatic. The *judgement* lane's
# two model configurations are written further down, after the model catalog
# sync -- they need registered profiles, which the sync is what guarantees.
tracking_policy_file="$state_dir/tracking-policy.json"
if [[ ! -f "$tracking_policy_file" && -f "$repo_root/deploy/phase9/p14a-tracking-policy-v1.json" ]]; then
  cp "$repo_root/deploy/phase9/p14a-tracking-policy-v1.json" "$tracking_policy_file"
  chmod 600 "$tracking_policy_file"
fi
# P14e / INT2: the three ProbeTemplates the ad-hoc research lane may bind. The
# manifest is publication material -- the owner publishes each template with a
# `human:` principal, and this script never signs anything -- so it is put
# where the owner can read it and nowhere a lane looks. The lane's own config
# file (research-task-lane.json) is *not* written here: it is the lane's
# switch, and switching the lane on before any template is published gives a
# lane that answers no_executable_adhoc_template_published every tick.
phase8_dir="$state_dir/phase8"
mkdir -p "$phase8_dir"
chmod 700 "$phase8_dir"
adhoc_templates_file="$phase8_dir/p14e-adhoc-probe-templates-v1.json"
if [[ ! -f "$adhoc_templates_file" && -f "$repo_root/deploy/phase8/p14e-adhoc-probe-templates-v1.json" ]]; then
  cp "$repo_root/deploy/phase8/p14e-adhoc-probe-templates-v1.json" "$adhoc_templates_file"
  chmod 600 "$adhoc_templates_file"
fi
# ADR-0005 / P9d-17a: the writer needs an approved extraction model
# configuration for drafting to run as mission automation.  Idempotent: appends
# the extraction routing policy only if its filters changed, writes the closed
# config next to the state, and points service.json at it.  No credential is
# read; the broker key path is referenced.
# P14-M: --tier cheap pins extraction to the cheap fallback chain rather than
# to one profile. Its long-standing pin, profile:deepseek-v4-flash, is that
# chain's first link, so the model that normally reads a window does not change;
# what changes is that DeepSeek being down stops losing the window.
# DALTON_EXTRACTION_MODEL_TIER= (empty) keeps the single pin.
extraction_tier=${DALTON_EXTRACTION_MODEL_TIER-cheap}
if [[ -n "$extraction_tier" ]]; then
  "$venv_dir/bin/python" -m dalton_core.document_extraction_setup \
    --config "$config_path" --tier "$extraction_tier"
else
  "$venv_dir/bin/python" -m dalton_core.document_extraction_setup --config "$config_path"
fi
# P14-M: make the router's model catalog agree with the broker's, append-only.
# The two had drifted -- five profiles Dalton offered that the broker no longer
# did, four the broker offered that Dalton had no profile for -- because
# nothing in the deploy ever reconciled them and deleting a stale profile would
# have broken the version chains that old route decisions resolve through.  A
# profile the broker has dropped now gets a *retired* version instead, and a
# profile the broker has added gets registered.  Idempotent: a re-install with
# no drift writes nothing.  Skipped without an OpenClaw config, because a Core
# installed without the gateway has no catalog to agree with.
if [[ -f "$HOME/.openclaw/openclaw.json" ]]; then
  if ! PYTHONPATH="$repo_root/src" "$venv_dir/bin/python" \
      "$repo_root/scripts/sync_openclaw_model_catalog.py" \
      --openclaw-config "$HOME/.openclaw/openclaw.json" \
      --model-router-db "$state_dir/model-router.sqlite"; then
    echo "model catalog sync failed; the router and the broker still disagree." >&2
    echo "Fix the OpenClaw config or the router, then re-run install.sh." >&2
    exit 1
  fi
fi
# P13k: the planner's model, only when the owner names one. It decides what the
# research works on next, so it routes through its own policy rather than
# sharing extraction's -- which pins a single profile by design. Left unset
# nothing is installed and the planner stays dark: its model costs fifty times
# the extraction model's, which is affordable for a few small calls a day and
# is not something anyone should acquire by upgrading.
#
#   DALTON_PLANNER_MODEL_PROFILE=profile:gpt-6-astra
#
# P14-M: naming a tier instead pins that tier's whole fallback chain, so an
# OpenAI outage falls through to the named alternative rather than losing the
# planning call. DALTON_PLANNER_MODEL_PROFILE still works and still wins.
#
#   DALTON_PLANNER_MODEL_TIER=brain
if [[ -n "${DALTON_PLANNER_MODEL_PROFILE:-}" ]]; then
  "$venv_dir/bin/python" -m dalton_core.research_planner_setup \
    --config "$config_path" --profile-ids "$DALTON_PLANNER_MODEL_PROFILE"
elif [[ -n "${DALTON_PLANNER_MODEL_TIER:-}" ]]; then
  "$venv_dir/bin/python" -m dalton_core.research_planner_setup \
    --config "$config_path" --tier "$DALTON_PLANNER_MODEL_TIER"
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
#   DALTON_DELIVERABLE_MODEL_TIER=brain   (the whole chain rather than one model)
if [[ -n "${DALTON_DELIVERABLE_MODEL_PROFILE:-}" ]]; then
  "$venv_dir/bin/python" -m dalton_core.deliverable_model_setup \
    --config "$config_path" --profile-ids "$DALTON_DELIVERABLE_MODEL_PROFILE"
elif [[ -n "${DALTON_DELIVERABLE_MODEL_TIER:-}" ]]; then
  "$venv_dir/bin/python" -m dalton_core.deliverable_model_setup \
    --config "$config_path" --tier "$DALTON_DELIVERABLE_MODEL_TIER"
fi
# INT3: the three lane switches that are model configurations rather than
# records. `research_planner_setup.install` already takes the policy id and the
# config file name as arguments -- that is why the deliverable setup is nine
# lines rather than two hundred -- so these blocks reuse it rather than adding
# two more modules that differ by two strings.
#
# Same gate as the planner and the deliverable: named, or nothing is written
# and the lane stays absent. A lane that is absent costs nothing; a lane that
# is switched on with no model refuses every tick and reads like a fault.
install_role_model_config() {
  # $1 policy id, $2 config file name, $3 profile ids (may be empty), $4 tier
  # The venv has dalton_core installed; no PYTHONPATH, same as every other
  # `-m dalton_core.*` call in this script.
  "$venv_dir/bin/python" - "$config_path" "$1" "$2" "$3" "$4" <<'PYROLE'
import json, sys

from dalton_core.research_planner_setup import PlannerSetupError, install

config, policy_id, config_file_name, profiles, tier = sys.argv[1:6]
profile_ids = [item.strip() for item in profiles.split(",") if item.strip()] or None
try:
    result = install(config, profile_ids=profile_ids, tier=tier or None,
                     policy_id=policy_id, config_file_name=config_file_name)
except PlannerSetupError as exc:
    print(json.dumps({"status": "rejected", "config": config_file_name,
                      "reason": str(exc)}), file=sys.stderr)
    raise SystemExit(1)
print(config_file_name + "=" + result["routing_policy_ref"])
PYROLE
}
# P14a: the judgement lane. Two configurations, written together or not at all
# -- the lane's own fragment already refuses a judge with no verifier, and a
# judge verified by its own model is not verified. The independence itself is
# checked at the route, before anything is paid for, and a judgement whose
# verifier shares the producer's family comes back `gated:same_family`; naming
# two different families here is what stops that being every judgement.
#
#   DALTON_EVENT_JUDGEMENT_MODEL_TIER=brain
#   DALTON_EVENT_VERIFIER_MODEL_TIER=verifier
#   (or DALTON_EVENT_JUDGEMENT_MODEL_PROFILE / DALTON_EVENT_VERIFIER_MODEL_PROFILE)
judgement_pin="${DALTON_EVENT_JUDGEMENT_MODEL_PROFILE:-}${DALTON_EVENT_JUDGEMENT_MODEL_TIER:-}"
verifier_pin="${DALTON_EVENT_VERIFIER_MODEL_PROFILE:-}${DALTON_EVENT_VERIFIER_MODEL_TIER:-}"
if [[ -n "$judgement_pin" && -n "$verifier_pin" ]]; then
  if [[ "$judgement_pin" == "$verifier_pin" ]]; then
    print -u2 "error: the event judge and its verifier are pinned to the same model ($judgement_pin); every judgement would be gated:same_family"
    exit 2
  fi
  install_role_model_config \
    "model-routing-policy:dalton-openclaw-event-judgement" \
    "event-judgement-model-config.json" \
    "${DALTON_EVENT_JUDGEMENT_MODEL_PROFILE:-}" "${DALTON_EVENT_JUDGEMENT_MODEL_TIER:-}"
  install_role_model_config \
    "model-routing-policy:dalton-openclaw-event-verifier" \
    "event-verifier-model-config.json" \
    "${DALTON_EVENT_VERIFIER_MODEL_PROFILE:-}" "${DALTON_EVENT_VERIFIER_MODEL_TIER:-}"
elif [[ -n "$judgement_pin" || -n "$verifier_pin" ]]; then
  print -u2 "error: set both DALTON_EVENT_JUDGEMENT_MODEL_* and DALTON_EVENT_VERIFIER_MODEL_* or neither; the judgement lane is installed as a pair"
  exit 2
else
  print "note: set DALTON_EVENT_JUDGEMENT_MODEL_TIER and DALTON_EVENT_VERIFIER_MODEL_TIER (different families) to install the event-judgement lane."
fi
# P12b: the claim index. One configuration, so all-or-nothing is automatic.
# The maintenance pool is the tightest of C2's four at 5% and this is where the
# index lands, so it is worth turning on deliberately rather than by default.
#
#   DALTON_CLAIM_INDEX_MODEL_TIER=cheap
if [[ -n "${DALTON_CLAIM_INDEX_MODEL_PROFILE:-}" || -n "${DALTON_CLAIM_INDEX_MODEL_TIER:-}" ]]; then
  install_role_model_config \
    "model-routing-policy:dalton-openclaw-claim-index" \
    "claim-index-model-config.json" \
    "${DALTON_CLAIM_INDEX_MODEL_PROFILE:-}" "${DALTON_CLAIM_INDEX_MODEL_TIER:-}"
else
  print "note: set DALTON_CLAIM_INDEX_MODEL_TIER to install the claim-index lane."
fi
# P9d-18 / ADR-0006: point the cockpit at the Core (read-only), the state
# directory, the heartbeat, the scheduler and the extraction model config so
# the owner's page can show progress, answer questions and draft goals.
"$venv_dir/bin/python" -m dalton_core.cockpit_setup --config "$config_path"
# INT2 / P14-M: tell the cockpit where the gateway's own model catalog is, so
# the page can say whether the models this Core holds are the models the broker
# offers -- and which way they differ, because "out of sync" on its own tells
# nobody what to do. A path rather than a convention: the control process must
# not go looking through the host's home directory on its own, and a Core
# installed without the gateway simply gets no catalog block.
if [[ -f "$HOME/.openclaw/openclaw.json" ]]; then
  "$venv_dir/bin/python" - "$config_path" "$HOME/.openclaw/openclaw.json" <<'PYBROKER'
import json, sys
from pathlib import Path

path, broker = Path(sys.argv[1]), sys.argv[2]
config = json.loads(path.read_text(encoding="utf-8"))
cockpit = ((config.get("control") or {}).get("config") or {}).get("cockpit")
if isinstance(cockpit, dict):
    cockpit["openclaw_config_path"] = broker
    path.write_text(
        json.dumps(config, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    print("cockpit.openclaw_config_path=" + broker)
PYBROKER
fi
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

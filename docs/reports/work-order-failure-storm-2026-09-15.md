# Work-order failure storm — chain halts, stale allow-lists, phantom spend — 2026-09-15

## What the owner saw

The trajectory page showed work orders failing end to end (`worker_completion`),
and 决定下一步做什么 (the plan lane) reporting provider failure.

## The four layers, in the order they stacked

### 1. The Claude CLI gateway hit its subscription weekly limit

Every `claude-fable-5-1` call returned `CLAUDE_CLI_ERROR: "You've hit your
weekly limit · resets 9am (America/New_York)"` (gateway log). The broker wraps
that error text as `INVALID_HOST_RESULT: host returned invalid text`.

### 2. That failure class halted every chain it touched

`INVALID_HOST_RESULT` contains "INVALID", so the classifier's catch-all read
it as `contract_violation` — a halting class. The brain chain's later links
never ran: plan, debate_map, dossier, event_judgement and language_revision
all "halted on contract_violation" at fable. Fix: `INVALID_HOST_RESULT` and
`HOST_COMPLETION_FAILED` classify as `provider_failure` (the host did not
answer; a quota wall is not a contract breach).

The cockpit's chain wrapper additionally downgraded any may-have-reached-
provider failure to `unclassified_failure` (halt). Host-frame failures are now
exempt there too — the same no-usage reading `thesis_impact_control` already
re-drives on.

### 3. Budget refusals are per link, not per chain

`budget_refused` was halting, so one flash model's provider input cap
(`PROVIDER_BUDGET_EXCEEDED: provider max_input_tokens`) stood down the whole
cheap tier for mornings at a time. It is a fallback class now: the next link —
bigger window, cheaper meter — is the move the chain exists to make.

Related: each host-frame failure was also **settling its reserved ceiling
against the day ledger** while delivering nothing — a quota-walled gateway
burning the pools without a single answer, which is the "花费在增长，进度没
有推进" the owner reported earlier. Host-frame failures now settle zero.

### 4. The tier allow-lists had gone stale under the tier chains

`filters.allowed_profile_ids` was written once, by hand, around that day's
chains; `publish_tier_selection` edited chains without ever touching it. Live,
all 16 policies' allow-lists were narrower than their own tiers, and the
cheap-tier members were missing everywhere — 369 straight
`MODEL_ROUTE_REJECTED` for the language check in one afternoon, no eligible
candidate at all. Fixes:

- `publish_tier_selection` now widens the allow-list to every model some tier
  of the policy can reach.
- Live repair: all 16 policies re-published (one tier save each), 21 model
  configs and two service.json pins repointed, and the writer plist's
  `--planner-routing-policy` moved off a v1 that predated the chains.

## Deployment note

The mid-day releases 88555f71/903dba6f were built without the optional-
dependency wheelhouse (openpyxl, pypdf, edgartools, yfinance missing — 48
suite errors/failures, all environmental). The final release 1762391766 is
built from commit 865ccfda with the dependency lock; full suite 8538 OK.

## Where it stands

- Language check and localization serve on `deepseek-v4-flash` (pos 1).
- fable's weekly-limit failure records `provider_failure` and the chain walks
  past it; guidepoint/web/search lanes green.
- plan retries on `RATE_LIMITED` from gpt-6-astra (OpenAI throttling) with
  fable quota-walled behind it. It recovers when OpenAI's limit does, or at
  9am ET when Claude's resets. Adding a third brain link (e.g. zai-glm-5-3,
  which recent chain versions carried) would remove this single point of
  provider capacity — an owner tier-page decision.

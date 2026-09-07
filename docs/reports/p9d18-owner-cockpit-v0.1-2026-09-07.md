# P9d-18: the owner's cockpit

*2026-09-07*  ·  ADR: [0006](../adr/0006-owner-cockpit.md)

## What the owner said

The cockpit was inhuman. What they need is one place to type the overall
research goal and see, under it, the goal in force, the sub-tasks the system
derived and their progress; a place to steer; a page that shows the research
log, what the system is doing; a page for ad-hoc questions; a page to approve
what needs a person. Clear, no machine language. Rebuild it.

## What was rebuilt

**One page, five views** (`cockpit_control.html`, served at `/`): 研究目标,
调整方向, 研究日志, 随时提问, 待你审批. The old review page moved to `/legacy`
with all its routes intact; operators and the existing tests keep it.

**A cockpit plane** (`cockpit_plane.py`, `/v1/cockpit/*`) behind the same
Tailscale identity, session and CSRF shell. It reads the Core read-only,
the lane tickets on disk, the heartbeat and the day-budget ledger, and holds
a small owner-only journal for jobs, drafts and the owner's own actions.
Every write goes through the writer as the owner's principal.

- *Overview*: the mission's title, objective, questions, deliverables and
  sources in the owner's words; per company, a stage in plain words
  (待开始, 搜集资料, 阅读中, 持续跟踪), counts of found, held, read and
  waiting documents, Claims and the three latest statements; totals; today's
  model calls and cost from the ledger, the web and AlphaEngine budgets from
  the heartbeat; the lanes' states and what is running right now.
- *Log*: one sentence per ticket (搜索资料, 获取网页, 获取研报, 阅读抽取,
  读取财报数据) with company and outcome, one per admitted Claim, one per
  closed review, one per owner action, and a problem line when a lane's
  heartbeat carries an error. Polled every six seconds; unseen entries badge
  the nav item.
- *Approvals*: undecided thesis admissions, capability promotions, planner
  proposals on live loops, forecast overturn candidates, and the owner's open
  drafts, each with a plain title and its decisions. Decisions call
  `decide_thesis_admission`, `decide_capability_promotion`,
  `bounded_planner_admit_proposal` and `decide_forecast_overturn`.

**A bounded model call** (`cockpit_model.py`) for questions and drafts: the
extraction routing policy, the same broker, an admission in the shared day
ledger bound to the active mission's caps, a scheduler WorkOrder so repeats
replay. No Core write.

- *Ask*: the question selects Claims (mentioned companies first, else all,
  bounded), the model answers from them only, citing tags; the answer shows
  the cited Claims, a confidence and the gaps.
- *Goal*: prose becomes a draft title, objective, questions and sub-tasks;
  confirmation publishes a new mission version derived from the current one
  with the coverage list, bindings, autonomy and budget unchanged. Companies
  the owner names that are not covered are listed, not added.
- *Steer*: a sentence becomes added or removed research questions, and a
  reworded objective when the sentence changes the goal; what the lever cannot
  do is said. Confirmation publishes a mission version.

**Steering reaches the model.** The mission's objective and questions now
enter the extraction context and prompt, so a new mission version re-keys
every window and the next drafts follow the new questions.

**Installation.** `cockpit_setup` writes `control.config.cockpit` (Core,
state, heartbeat, scheduler, journal, model config); the installer runs it.

## Verification

- Tests (`tests/test_cockpit_plane.py`): the overview reads goal, progress
  and activity without refs; a question is answered from Claims with
  citations, admitted in the ledger under the mission, and replays on repeat;
  a goal draft publishes version 2 only on confirmation with the exact hash,
  leaves the coverage list alone, and its questions reach the extraction
  prompt; a steer draft adds and removes questions and reports what it cannot
  do; a thesis candidate is decided through the writer with the candidate's
  hash; a ledger below one reservation refuses before any model call; setup is
  idempotent; fenced or prose-wrapped JSON is unwrapped.
- Live, after deploy, through the shell with the owner's identity: the page
  serves; the overview shows mission v7, five companies with their counts,
  two lanes running, model calls and cost for today from the ledger; the log
  lists fetches, extraction runs, closed reviews and failures in plain
  sentences with company labels; approvals are empty.
- Full suite: only the two known macOS path failures and one node broker
  round-trip test that passes when run alone.

## Not in this slice

Changing the coverage list, connecting a source, raising a budget and adding
a tool are not levers the cockpit has; the steer draft says so. Each needs
its own authority change. The legacy page is unchanged.

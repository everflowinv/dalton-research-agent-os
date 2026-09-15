# Crowd-child starvation, xreach clobber, and trajectory fixes — 2026-09-15

## What the owner saw

1. 监测散户舆情与职场评价、形成核心投资建议并提交人工决策、校验行业与公司模型勾稽并推进研究阶段 all showed 暂时不可用 / `unavailable:RemoteError`.
2. 检索第三方专家访谈纪要库 still showed `quota_exhausted` after the v2 quota raise.
3. Clicking 查看模型与预算 opened 执行轨迹.
4. The trajectory page rendered literal `<span …>` text, `未登记模型` for every model call, and broke its own layout seconds after opening.

## Root causes and fixes

### The crowd child starved the writer's single request executor

`dispatch_mission_crowd_sources` runs its three host-tool children synchronously
inside one writer request; `STORE_REQUEST_TIMEOUT` is 30 s. The X child was
spinning in an infinite self-exec loop (below), so the op ran past every
deadline, and every op queued behind it — conviction call, stage bridge,
agenda-feedback reads — timed out with it. That is why three unrelated lanes
read `unavailable:RemoteError` at once.

Fix (`mission_crowd_source_lane.py`): crowd children are killed at
`CROWD_CHILD_TIMEOUT_SECONDS` (20 s), and once a tick has spent
`CROWD_TICK_CHILD_BUDGET_SECONDS` (10 s) on children the remaining sources
defer to the next tick. Worst case 10 + 20 = 30 s, exactly the request budget.

### The xreach CLI had been overwritten by its own wrapper

`/opt/homebrew/bin/xreach` → `~/.openclaw/tools/node/bin/xreach` →
`…/xreach-cli/dist/cli.js`, and `dist/cli.js` (mtime 2026-09-15 10:14) held the
Dalton host-tool wrapper, which execs `/opt/homebrew/bin/xreach`: a closed
loop. Writing the wrapper through the symlink chain clobbered the real entry.

Fix (host, not repo): `dist/cli.js` restored from the package's own `src/cli.ts`
(the package ships its source); the clobbered copy is kept at
`~/.dalton/xreach-cli.js.clobbered-wrapper-backup`. `xreach --version` → `0.3.3`
exits promptly. Verified live: the crowd lane records again (blind 60 dupes,
xueqiu 10 dupes), and conviction/stage-bridge answer instantly and idle.

### Guidepoint: the plan cap, not the governed quota, was binding

`budget()` takes `min(governed, planned)`; the governed quota was raised to
500 but `us-it-services-guidepoint-v1.json` still said `max_calls_24h: 20`,
and trailing-24 h usage was exactly 20. The plan validator caps the field at
200, so the plan was raised 20 → 200 with its `content_hash` re-bound through
`validate_guidepoint_discovery_plan`. Takes effect on writer restart.

The quota raise also surfaced a second Guidepoint failure: the search
authorities re-register the v1 rate policy under the immutable idempotency
key `guidepoint-search-library:rate-policy:v1`, whose recorded request hash
still describes the 25-a-day constants, so every dispatch after the raise
died on `ConnectorConflict`. `ensure_governed_authorities` now adopts an
active policy whose ceilings are at least the code's own — the same
accommodation the host-tool runner already ships. The out-of-band v2
(500/day, activated 13:17 UTC) satisfies it. Regression test in
`tests/test_guidepoint_search_lane.py::ExecutorTests`.

### The xreach child could not find node under launchd

The writer's services run on launchd's PATH, which has no `node`; the
restored `cli.js` shebang is `#!/usr/bin/env node`, so the X child exited
127 (`env: node: No such file or directory`). The host tool at
`state/dalton-core/host-tools/xreach` is now a real wrapper that puts the
openclaw node on PATH before exec'ing the CLI (verified with `env -i`).

### The trajectory refresh hijacked other reader views

`trajectoryRefreshLoop` passed the outer `#reader` container into
`renderTrajectory`, whose reset path `replaceChildren`s its argument — deleting
`#reader-sheet` (layout loss) and, when a fetch resolved after the user opened
another view, overwriting that view (the 模型与预算 → 执行轨迹 symptom).

Fix (`cockpit_control.html`): every reader view now stamps
`sheet.dataset.viewSeq`; `renderTrajectory` aborts if the stamp changed, the
refresh loop targets `#reader-sheet` and stops when the reader is closed or the
view was replaced, and `closeReader` clears the timer. The per-row status dot
is built as a real element instead of literal HTML text.

### Trajectory model names came from an empty catalogue

`trajectory()` called `_model_display_name(profile_id, {})` — every call read
未登记模型. It now reads the same "current version of every profile" the models
page reads, from the router DB it already holds open. Regression test in
`tests/test_cockpit_plane.py::TrajectoryTests`.

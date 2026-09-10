# SEC 8-K discovery proposal — 2026-09-10

## Decision for the owner

Approve or reject replacement of the active on-disk SEC discovery manifest with
`discovery-plan:us-it-services:sec-filings:2` (hash
`a660a9e1b863ffe1a9afe526c5005710d0f7d82f907caf60a41b1a14c9e7b388`). The
candidate preserves the existing five-company universe, 50-call trailing-day
budget, and 10-K spec byte-for-byte, and adds only:

```json
{"form":"8-K","lookback_days":30,"rediscovery_interval_days":1,"retry_interval_days":1,"spec_ref":"current-report-8k"}
```

This is a proposal. It has not been copied to the live state, published,
signed, or used to run SEC discovery.

## Actual authority topology

The discovery plan is a closed, hash-bound schema 0.4 file loaded by the
writer. There is no discovery-plan version table or pointer in Core SQLite.
At launch, the coordinator resolves the active CoverageMission and asks
`CoverageMissionAuthority.authorize_source_discovery` for an exact mission
version/hash binding. The current live bindings read on 2026-09-10 were:

- prior plan `discovery-plan:us-it-services:sec-filings:1`, hash `27a19e8…`;
- mission `coverage-mission-version:us-it-services:13`, hash `d59edcb9…`;
- approved governance `connector-governance:sec-filings-index:v1`, hash `1f8acea7…`.

The mission already lists `source:sec-edgar` as connected and permits
`source_discovery`. The approved SEC filings-index capability already accepts
a plan-provided form. Therefore this proposal does not change the mission or
connector governance contract. The review bundle records `governance_change`
as null rather than proposing an unnecessary capability expansion.

## Artifacts and validation

`scripts/build_sec_8k_discovery_proposal.py` is a reusable read-only builder.
It validates the active plan, refuses an existing 8-K form or duplicate spec,
derives the numeric next plan id, and emits both the runtime-valid candidate
and a hash-bound review chain. It reads the active mission through SQLite
read-only mode, creates a temporary SQLite backup, and exercises the real
mission authorization path there before writing proposal files.

- Candidate: `deploy/phase10/p10-us-it-services-sec-filings-plan-v2.candidate.json`
- Review chain: `deploy/phase10/p10-us-it-services-sec-filings-plan-v2.review.json`
- Proposed owner action: replace the writer's SEC plan argument with a copy at
  `discovery-plans/us-it-services-sec-filings-v2.json` only after approval.

Focused validation passed: `python3 -m unittest tests.test_sec_8k_discovery_proposal`
and the existing SEC discovery/plan tests. No network call was made.

## Limits

The 30-day lookback supports daily current-report discovery while bounding the
first approved catch-up. The existing SEC adapter and downstream acquisition
remain responsible for deduplicating filings by accession. Approval enables
discovery of all 8-K current reports; downstream parsers decide which archived
documents contain earnings releases or buyback authorization text.

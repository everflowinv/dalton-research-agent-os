# R14a runtime acceptance and observed product gaps

R14a `e22fdf617474dd82705a83a08be8c8a31bad17f8` is deployed and published as the verified release. Deployment ran 18:39:47–18:43:10 UTC, exit 0. The controller started at 18:41:47 UTC, PID 23919. All 45 observations passed over 672.145 seconds on that same process; final installed/configuration verification passed at 19:03:40 UTC. These are runtime acceptance results, not a claim that all research products are complete.

## Acceptance evidence

- Frozen full suite: 7,463 tests, zero failures/errors, one skip; runner elapsed 718.169 seconds. Log SHA-256 `aea00fef9c3d50a5053b3398fa906bc971e1e365c6b380c9b0b3de1943174995`.
- Wheel SHA-256 `1ce70a0f5fdfd7c92814cef5f1b7adfbbba0eb74c7aea2edd38e6c1e6e42728c`; all 602 runtime files match, with three JavaScript syntax checks.
- Fresh copied-state rehearsal: 14 steps, 42 tick entries, zero escaped calls. Execution is bound to clean separate ops checkout `22fe2ffab85621e69e57c5ce172d5b9c94d29e0b`.
- Deployment receipt SHA-256 `5f2fcd0dcea1a1fcdcc498610ed7c0e721faac36882c84c7d6b646fc111009b6`; installed verification `7a023fd355d694b304d9a372baca0c252bfd6919c0ce019bd654a1e752cd2915`; health summary `8c4dd119d256bcb9c46792dbeaccfe57d95155833d6c2d0af8c8ca82a53f4fc9`.
- All 17 model configs, service config and mission v14 are preserved. The reviewed OpenClaw changes update the broker frame limit and managed web-search plugin; current Gemini selection is retained. Two historical pending requests are preserved without replay/refund.
- The private packet is `dalton-owner-activation-20260910/foundation-r14a-release`. Its rollback directory `deploy-rollback-20260911T183949.820888Z` and database snapshot `20260911T183956.641050Z` are retained. R14's earlier failed full-suite result remains failed and was never deployed.

## Actual normal-queue observations

The first post-start planner work completed in about 77 seconds. Its exact plan `mission-research-plan:e1a517a43b8d1573c6a557831a3d78e6` has 11 directives, four inquiries and three directed-document inquiries. The submitted prompt retained 117 held document identities within the configured input budget. The budget ledger settled 910,010 micros; this is recorded internal cost, not a vendor invoice assertion.

Gemini search `web-search-discovery:678627a033ce8d31b4c3db33` successfully returned ten document references in one provider call. Local registration then failed: a saturated ranked page correctly carries source status `partial`, but the URL-authority consumer accepted only complete/empty pages. The result and raw source remain held. The next repair accepts this bounded ranked response and completes only local registration from exact held proof, without another paid search. Historical failed summaries remain historical; recovery appends the missing discovery/document records.

The three directed inquiries have not reached execution: the document executor is configured but its admission producer is absent. A read-only audit verified same-mission acquired originals and the existing mission's research_task/model_run/stage_record grants. The next repair connects a directed-only producer under the document lane switch, with a real local subprocess/Unix-broker regression. Ordinary ad-hoc inquiries remain governed by their own configuration.

## Next steps

1. Finish independent review of local search recovery, including exact durable journal proof and fair bounded traversal of old failures; finish configurable directed-document admission.
2. Freeze and accept R14b for these observed execution gaps; deploy under the owner's existing authorization and verify actual search registration and directed research output.
3. Complete the separately integrated company-specific financial structure, annual/quarter forecasts and strict AMZN-format exporter. The current template contract alone is not visual acceptance, and an income-statement bridge alone is not complete balance-sheet/cash-flow coverage.
4. Continue actual prompt/parser failures and remaining foundation backlog. Prepared new-source approvals stay unsigned; current Cockpit visual style is retained.

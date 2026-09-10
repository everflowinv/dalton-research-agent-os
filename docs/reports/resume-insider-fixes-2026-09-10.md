# Insider / buyback follow-up fixes

Date: 2026-09-10  
Branch: `resume-insider-fixes`  
Base: `w4-insider-buyback` at `e529cf5`

This follow-up closes F6, F7, and F8 from §6c of the parallel development
plan without taking the unapproved D8 window change or renaming any buyback
payload fields.

## Changes

- F6: restored `p14a-tracking-policy-v1.json` byte for byte (SHA-256
  `714e752c96bb5d66eef4249216267c13ee52cf4a81bc31067b0fecfadb89bc10`).
  The new `sec-ownership` cadence and expanded SEC rationale now live in
  `p14a-tracking-policy-v2.json`, whose identity is
  `tracking-policy:p14a:v2`. Runtime defaults and fresh-install seeding point
  to v2. The installer still refuses to overwrite an existing
  `tracking-policy.json`.
- F7: the judgement scheduler groups issuer-purchase monthly rows by company,
  kind, and accession. The rows remain separate ResearchEvents and appear as
  a table in one prompt, but consume one company slot and one judge/verifier
  pair. The verified result marks every row in the group processed. A test
  pins the practical case: three Item 2 rows plus one Form 4 use two calls and
  leave no event unjudged.
- F8: an absent sale-plan match becomes `false` only when the company has at
  least five `management_and_capital_allocation` Claims. Below five it is
  `unknown`, with `coverage_thin` and the observed count in the reason. A
  positive matching Claim remains `true` even under thin coverage.

## Owner deployment checklist

1. Review and sign `tracking-policy:p14a:v2` before replacing the live
   `tracking-policy.json`; its content hash differs from v1 by design.
2. Preserve the live v1 file until that signature is recorded. Fresh installs
   seed v2, while reinstalling an existing Core leaves its policy untouched.

## Verification

- Focused: `PYTHONPATH=$PWD/src python3 -m unittest` over insider context,
  judgement lane, buyback disclosure, tracking cadence, service installer,
  mission tracking, and deploy rehearsal tests: 315 passed.
- Import smoke check for `event_judgement`, `event_judgement_cli`, and
  `insider_context`: passed.
- `git diff --check`: passed.
- First full discovery run: 5,480 tests, two stale v1 expectation failures,
  one skipped. Both expectations were updated to v2; a clean full rerun is
  required before integration sign-off.

## Remaining boundaries

- F11 payload unification belongs to integration and is intentionally absent.
- The D8 60-trading-day window is not approved and is intentionally absent.
- The original slice's 8-K fetch permission and Item 5 parsing gaps remain.

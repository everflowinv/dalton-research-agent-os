# P9d-17b: drafts become Claims without a person in the loop

*2026-09-07*  ·  ADR: [0005](../adr/0005-autonomous-document-extraction.md)

## Where P9d-17a left things

Drafting is live. After the owner published the budget-bearing chain (policy-5,
mandate v2, constitution v3, mission v5), the first children made real model
calls through the broker at roughly a tenth of a cent each, settled against the
mission's day budget, and left persisted suggestions the cockpit reads back.
Suggestions were still only suggestions: staging and admission were human ops.

## What this slice changes

**An automation review scope.** `TranscriptCorrectionAuthority` gains
`automation_verified_raw_span`, the mirror of the human `verified_raw_span`:
the span is verified byte-exact against the original the same way, the actor
must be an `automation:` principal (a person cannot publish it, and automation
cannot publish the human scope), and the rationale names the model invocation
and route the span came from. Citation binding and resolution treat the two
scopes identically. The contract enum and actor pattern follow.

**A policy rule for qualitative document candidates.**
`research-auto-commit:mission-document-qualitative:v1` in
`research_candidate_auto_commit.rules` lets the policy evaluator admit a
qualitative candidate when, and only when: producer and evidence actor are the
same `automation:` principal; no number is asserted (value, unit, scale,
currency null; no digit, percent or dollar sign in the statement); the evidence
is authenticated transcript evidence from `source:alphaengine`; the citation
binding is persisted, exact, and claim-eligible; its correction set carries the
automation scope by the same principal; and the SourceEnvelope is the exact
acquired `get_document` envelope. Revised or chained candidates still escalate.
The shared Ledger writer accepts a qualitative candidate on the policy path only
under this rule; ADR-0003 B's human path is untouched.

**Reviews close themselves.** `resolve_document_review` accepts the review's
own mission principal as well as a person. A fully drafted review whose
suggestions were admitted closes as `extraction_staged` bound to the first
candidate, with a rationale counting admitted and refused suggestions; one with
no admissible suggestion closes as `dismissed` with the reason. A window held by
a gate (missing grant, missing policy rule, web source) leaves the review open.

**The child does the work.** After drafting, `document_extraction_cli` runs an
admission pass over every review whose windows are all drafted: for each
suggestion it publishes or reuses the correction set, binds the citation,
stages the qualitative candidate, and promotes it through
`commit_policy_candidate`. Every step is idempotent, so a re-run reports
duplicates and writes nothing. `formal_authority_writes` now counts the
Evidence and Claim versions actually written. The child gets the shared
candidate staging database through `--candidate-staging`, passed by the
launcher from the writer's configuration.

**Rate.** Four windows per tick instead of two. At a tenth of a cent a window
the daily cap is not the limiter; the tick is.

## What the owner runs once more

The policy must list the rule, and a policy change cascades through the
constitution and the mission because both bind it by hash. The same script
does it, rebinding only what changed (the mandate already carries the budget
and is left alone):

```
cd /Users/everflow/Projects/dalton-research-agent-os
.venv/bin/python scripts/publish_extraction_authority_chain.py --live \
  --add-auto-commit-rule research-auto-commit:mission-document-qualitative:v1
```

Rehearsed on a copy of the live Core: policy-6 lists the rule next to the two
SEC company-facts rules, the outer-budget check binds policy-6, and automation
is granted on both sources under mission v6. Until it runs, every admission is
held with "active governance policy does not list …" and nothing is staged.

## Verification

- Automation scope: a person cannot publish it, automation cannot publish the
  human scope, a citation inside the span binds and is claim-eligible, one
  beyond it is refused.
- End to end with the real child in hermetic mode: without the policy rule the
  window is held and the review stays open; with it, the fixture draft becomes
  one Evidence and one Claim version (qualitative, no value, formal actor the
  policy reviewer, candidate produced by the mission automation), the review
  closes as `extraction_staged` bound to the candidate, a re-run scans nothing
  and writes nothing, and admitting a resolved review is refused.
- A candidate whose page envelope is partial (multi-page acquisition) is
  admitted; the Ledger writer's exact-binding check, not envelope status, is
  the guard.

## Not in this slice

Public-web pages: drafted, not staged (P9d-17c needs a citation authority for
web sources). Thesis admission: still human (ADR-0001), pending its own ADR.

## What the first eighteen live windows said

Thirteen were rejected by the output contract and the five that passed were
empty. The broker journal shows why: the model wraps its JSON in a markdown
fence, which the strict parser refused, and when it does produce a statement it
names the period ("by the end of 2026") or a figure ("79%"), and the contract
refused any digit at all.

Three changes, none of them to the contract's authority:

- `unwrap_model_json` strips one surrounding fence before the strict parse. The
  persisted text is untouched; anything else non-JSON is still refused.
- `statement_asserts_a_value` replaces the bare digit ban. A year, quarter,
  half or fiscal-year label is a period, not a value; every other digit,
  percent or currency sign is a value and is refused. The policy evaluator uses
  the same check.
- The prompt asks for raw JSON, one reported view per suggestion in one or two
  sentences, no numbers, and says period labels are allowed.

A rejected window is terminal on purpose. The prompt is not part of the task
hash, so those thirteen stay rejected under their current context ids. The
second chain publish re-keys every context through the new mission version, so
every window is drafted again under the new prompt, at about a tenth of a cent
each.

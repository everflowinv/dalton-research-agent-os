# ResearchPlan registered annual-report qualitative contract

Date: 2026-09-11
Status: development candidate; isolated tests only; not deployed

## Result

`ResearchPlanVersion` `0.2` now defines a closed four-node qualitative pipeline for `search_registered_annual_report`:

1. `registered_filing_retrieval`
2. `qualitative_model_draft`
3. `independent_qualitative_verifier`
4. `qualitative_candidate_staging`

The operation is question-agnostic. The approved question, exact retrieval proof, routed draft, independent verifier result, source/context hashes, route decisions and model invocation identities are carried into the derived downstream WorkOrders. Draft and verifier token, cost, time, attempt, policy and credential-slot limits are explicit plan parameters and therefore part of plan identity.

## Source authority

A caller supplies only the Core mission/company/review/accession pointer, query terms, explicit limits and optionally an append-only `document_read_completion_proofs` ref. Plan creation derives all acquired-document and source fields through the existing Core authority path. It requires:

- the exact acquired `coverage_mission_discovered_documents` row and fetch ticket;
- the exact `coverage_mission_document_reviews` association;
- the exact company/accession `coverage_mission_statement_filings` 10-K row;
- when supplied, an append-only document-read proof whose contiguous successful windows all bind the same rendered content hash;
- the completed public-web fetch manifest, connector invocation/profile/call, successful physical attempt, usage and consumed quota settlement;
- the exact source envelope, raw artifact, owner-only spool bytes, raw byte hash/count and deterministic full rendering (`source_truncated=false`);
- an official SEC host and canonical URL containing the exact compact accession.

The registry has no local-path registration API. It re-runs the manifest/receipt/spool/render verification before every search and compares the derived manifest, raw and rendered hashes to the immutable plan. An arbitrary local file paired with a real acquired Core row cannot become a registered annual report.

The whole-document read proof is optional. A fully acquired, receipt-verified and non-truncated annual report can be searched directly without first completing a general reading pass. When supplied, the plan binds that append-only proof instead of the mutable current review hash. Normal review progress can change `state` and `updated_at` without invalidating an already approved plan; the proof retains the exact historical review snapshot and source hash used when all windows completed.

A separate SELECT-only production audit packet confirms that this authority
shape already exists for EPAM filing `sec:filing:0001352010-26-000015`:
`document-read-proof:221bd4cfd1dd0eaa5805a28fcc57955b` binds 35 successful
contiguous windows covering 419,086 characters, manifest
`public-web-fetch-manifest:aa7af85cb44c655555b2a51172240305603e90fe41b9ecddc9871509e847832d`,
rendered source hash
`b7a7cb7a2d7169f740d32f2b712158f731401b1a33c2ac15cd57af3f04690f0e`
and `source_truncated=false`. The audit made no model call or authority write.

The accession prefix is deliberately not issuer proof. SEC documents that the first accession segment identifies the submitting entity, which can differ from the issuer covered by the filing: [SEC EDGAR access documentation](https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data). Issuer authority comes from Core's exact statement-filing company/accession/CIK binding.

## Model and staging authority

Both model nodes run through the existing Scheduler, ModelRouter, broker adapter and durable usage/cost accounting. Prompts contain the complete approved question, exact retrieval material and complete closed output schema. The verifier WorkOrder binds the producer family and route; its routing policy requires a different model family. A passing result must preserve the exact proposed statement and return no error findings.

The integrated runtime also binds the closed `provider_retry` policy for each
model stage into the version 0.2 plan and derived WorkOrder. Paid retry routing
uses the worker's actual capability, purpose tier and producer authority. The
verifier re-reads the immutable producer route decision and derives its family
from that decision before every initial, retry or fallback route; caller text
cannot relax family independence. The registered purposes are selectable in
the Cockpit as `registered_annual_report_draft` on the brain tier and
`registered_annual_report_verifier` on the verifier tier.

`SecCompanyFactsLane`, the existing production constructor for
`ResearchPlanExecutor`, now accepts an all-or-none annual-report runtime. It
constructs the manifest/receipt/spool registry, passes that same registry to
`ResearchPlanAuthority` and `ResearchPlanExecutor`, constructs distinct draft
and verifier workers, and passes each plan-bound retry configuration to the
real worker. Partial runtime wiring is refused during construction. This removes
the earlier test-only pattern of assigning registry and workers after the
executor had already been built.

The final node reuses `CandidateStagingStore` and its existing provenance consumers. It creates draft-only candidate Evidence and Claim records backed by the registered SEC authority and passing independent verifier proof. It does not write formal Evidence, Claim or Thesis rows, approve a plan, start one automatically, sign anything, perform acquisition or deploy changes.

The existing `0.1` numeric `list_filings` and `get_company_facts` identities, validation, four-node WorkOrders and execution behavior remain unchanged.

## Remaining retry boundary

This slice does not claim that every model failure is recoverable. Exact
adapter-authenticated returned provider failures can retry once per configured
profile and then advance through the declared chain within the WorkOrder's
attempt and budget bounds. A post-send timeout whose completion is unknown
still settles conservatively and remains terminal today.

The required follow-up is a separate, configurable recovery contract for that
unknown-completion case: charge the old attempt at its conservative full
reservation, create a fresh WorkOrder and fresh invocation under an explicit
retry count/cost/time budget, and never replay or refund the unknown old call.
That recovery needs its own immutable lineage and idempotency boundary before it
can be enabled; it is outside this integration commit.

## Integration verification

The combined annual-report and returned-provider-failure branch was tested from
the `c29b3e2` integration base. The Python suites covered 287 cases across the
versioned plan, numeric executor, annual four-node execution, production lane
construction, provider retry, transcript worker, router/fallback selection,
Agenda, Human Intent, host adapter and workspace process. The OpenClaw broker's
24 Node tests also passed. The annual end-to-end case uses an accepted and
started plan, real Scheduler/ModelRouter/accounting paths, two paid returned
failures, same-profile retry, tier fallback, same-family verifier rejection and
draft-only CandidateStaging completion without network access.

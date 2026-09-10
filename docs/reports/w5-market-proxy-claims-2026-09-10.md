# W5 F10 — market proxy claims

Date: 2026-09-10  
Baseline: `d6206b4`  
Branch: `w5-market-proxy`

## Result

ClaimIndex entries now carry an `evidence_kind`. Existing records decode as
the ClaimIndex-only compatibility sentinel `statement`; their stored
`record_json` and `content_hash` remain unchanged. New values validate against
`statement` or the shared evidence-kind vocabulary, and proxy production uses
the shared `MARKET_PROXY` constant.

`MarketProxyClaimAuthority` derives evidence only from the latest settled,
governed `MarketPriceSeriesVersion` selected by an explicit closed mapping.
The immutable proxy record discloses target, source series identity, original
version ref and hash, Yahoo source, date, value, unit, and `proxy_gap`.
Provisional bars are refused until settlement.

The proxy cannot become a company actual through legacy readers. Its Core
Claim is deliberately qualitative with null `value` and `unit`; the numerical
payload remains in the separate proxy authority. Tests show guidance actuals
and numeric-conflict discovery do not admit it. Its EvidenceVersion lineage
binds the original price version and hash, and its ClaimIndex entry says
`market_proxy`.

The existing market-price lane is the runtime producer. When an approved
yfinance connector and an explicit proxy config are supplied, the lane adds
the configured source series and ticker to its local acquisition candidates.
It does not mutate the mission, and rejects mappings whose target is outside
the active mission. Empty or absent mappings do nothing. The proposed
deployment config is empty and therefore cannot activate collection.

The model-spec consumer joins current proxy authority records through current
ClaimIndex entries. It carries them into the hashed company state and prompt,
including the source version/hash and mandatory gap. A refreshed source series
therefore changes model-spec state; proxies remain separate from filed
statement rows and the prompt labels them as never company actuals.

The new authority schema is registered in bootstrap and deployment rehearsal.
No new lane or model purpose was added; derivation itself has zero model cost.

## Validation

Focused ClaimIndex suite: **137 tests passed in 1.005s**.

Combined runtime, migration, ClaimIndex, service, market-price, model-state and
model-spec suite: **364 tests passed in 3.537s**.

The dedicated seven tests cover empty config, closed mappings, settled and
provisional series, immutable provenance, ClaimIndex kind, actual exclusion,
numeric-conflict exclusion, state/prompt consumption, explicit source
acquisition, mission preservation, and rejection of targets outside mission.

## Deployment

`deploy/phase9/proposed-market-proxy-mappings-v1.json` is deliberately empty.
Activation requires an owner-reviewed mapping file plus the already-required
approved yfinance governance record. This branch does not sign, install, or
activate either item.

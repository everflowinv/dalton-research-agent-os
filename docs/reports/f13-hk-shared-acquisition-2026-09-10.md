# F13 HK shared daily acquisition

## Result

`daily_buyback_tape` is a fifth, independently governed HKEX capability with
day-scoped `{as_of}` input. It is not a fifth scheduler task. The existing four
company operations and their committed governance files remain unchanged.

When an approved daily-tape record is installed, a
`next_day_disclosure_returns` child acquires the full-market workbook once and
then filters that capture locally for its requested `hk_ticker`. Every company
view for the same acquisition carries the same acquisition invocation and
artifact, plus a distinct `derived_view_ref` that binds the company operation
and its unchanged per-company parameters. With no daily-tape record, the
existing per-company path remains available.

## Cache and refusal boundaries

The cache key binds operation, day, derived HKEX URL, governance ref/hash,
source hash, and schema hash. An interprocess file lock covers the check and
write. Source bytes and the owner-only manifest are written atomically, and a
manifest is published only after the raw bytes, canonical capture artifact,
daily output contract, and acquisition invocation all succeed.

On a hit, the child revalidates the manifest identity, source-byte hash,
canonical capture hash, and persisted spool artifact. Any missing or changed
part fails closed before a company wire or event is published. Governance is
loaded and validated before cache access, so a cached approved read cannot be
used through an unapproved or mismatched record.

## Deployment state

`deploy/connector-governance/hkex-filings-daily-buyback-tape-v1.json` is a
proposal. It is deliberately unseeded: enabling the optimized path requires
the owner to approve and install this new capability. This change performs no
approval, signing, live installation, or deployment.

## Validation

Focused coverage exercises two company views concurrently and after restart,
day and governance cache partitioning, corrupt-cache refusal without refetch,
unapproved-record refusal before fetch, original fixture replay identity, the
four existing operation contracts, generated connector inventory, and the
deliberately-unseeded deployment inventory.

The existing HK lane's in-memory failure counters remain outside this slice;
failure-ledger migration is tracked separately.

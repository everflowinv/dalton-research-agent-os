# Historical uncertain-spend correction — 2026-09-10

The immutable settlement row remains unchanged. A new append-only correction binds the exact admission and settlement, an evidence reference and SHA-256 hash, the operator actor, and an idempotency key. It can only increase effective spend and cannot exceed the original reservation. Day caps, mission/outer admission checks, and pool reporting read the corrected effective amount.

A bounded recovery operator packet should separately bind the old failed WorkOrder and formal result, the correction ref/hash, repaired host release hash, current route/contract hashes, and a unique recovery epoch. It may issue one fresh WorkOrder only after the correction exists. It must never replay the ambiguous invocation or automatically scan and redrive failures.

Validation: 148 focused adapter, Cockpit fallback, shared-capacity, day-budget, and pool tests passed.

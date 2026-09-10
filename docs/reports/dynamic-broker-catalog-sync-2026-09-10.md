+# Dynamic broker catalog profile versioning — 2026-09-10

The OpenClaw catalog sync now treats the broker profile ID as the stable cockpit and policy reference. A changed provider/model route, public price, context window, output limit, family declaration, or capability declaration appends the next immutable ModelRouter profile version under that same ID. Existing profile rows and historical route decisions are not rewritten.

Only public catalog fields are projected: provider/model IDs, context/output limits, published prices, broker maxTokens, and optional profile family/capabilities. Provider credentials and plugin secrets remain outside every profile and report. An unknown broker profile defaults to ordinary research capability with an unclassified provider family; it does not receive research-hard. Existing curated profile roles survive a route rename, while model family is reset to unclassified unless the broker profile explicitly declares the new family.

A provider change also changes the logical credential-slot reference. If the current provider directory omits either price, a curated route no longer retains its bootstrap price: it receives the same conservative ceiling and `unpriced` marker as a dynamic route, including the existing last-link restriction.

Independent verification now fails closed whenever either producer or candidate family is unclassified. Explicit, validated family metadata is therefore required before a newly cataloged model can serve as an independent verifier.

A read-only projection of the current local public catalog found one concrete activation gap: `profile:deepseek-v4-flash` now names a different catalog alias and has no explicit family or capability metadata. Sync will preserve its research role but classify its lineage as unknown. If selected as producer or verifier, independent verification will refuse until an owner-reviewed public family declaration is persisted through the model-selection configuration path. This review did not infer the alias family or modify host configuration.

`catalog_sync_status` builds the same desired public profiles as the writer path and compares semantic content, so check-only detects route, price, capacity, capability, and family drift without writing. Sync handles add, update, retire, and revive as append-only transitions. A second sync with unchanged public catalog data writes no rows.

Focused verification covers a populated real ModelRouter, a stable profile whose route, price, capacity, family, and capabilities all change, preservation of the prior row bytes, read-only drift detection, idempotent second sync, secret-free reporting, retirement/revival, and unclassified-family verifier refusal.

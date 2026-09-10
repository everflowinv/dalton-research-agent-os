+# Dynamic broker catalog profile versioning — 2026-09-10

The OpenClaw catalog sync now treats the broker profile ID as the stable cockpit and policy reference. A changed provider/model route, public price, context window, output limit, family declaration, or capability declaration appends the next immutable ModelRouter profile version under that same ID. Existing profile rows and historical route decisions are not rewritten.

Only public catalog fields are projected: provider/model IDs, context/output limits, published prices, broker maxTokens, and optional profile family/capabilities. Provider credentials and plugin secrets remain outside every profile and report. An unknown broker profile defaults to ordinary research capability with an unclassified provider family; it does not receive research-hard. Existing curated profile roles survive a route rename, while model family is reset to unclassified unless the broker profile explicitly declares the new family.

Independent verification now fails closed whenever either producer or candidate family is unclassified. Explicit, validated family metadata is therefore required before a newly cataloged model can serve as an independent verifier.

`catalog_sync_status` builds the same desired public profiles as the writer path and compares semantic content, so check-only detects route, price, capacity, capability, and family drift without writing. Sync handles add, update, retire, and revive as append-only transitions. A second sync with unchanged public catalog data writes no rows.

Focused verification covers a populated real ModelRouter, a stable profile whose route, price, capacity, family, and capabilities all change, preservation of the prior row bytes, read-only drift detection, idempotent second sync, secret-free reporting, retirement/revival, and unclassified-family verifier refusal.


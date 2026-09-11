# Typed financial-note evidence authority

This slice adds a read-only authority for the diluted-EPS-numerator note target.
It resolves only results whose typed target was present in the immutable planner
inquiry before admission and whose directed-document candidate was canonically
promoted. An older qualitative result cannot be relabelled as financial-note
evidence.

The resolver replays the immutable admission, coverage mission, SEC 10-K
statement filing, acquired-document registration, registered range-search
proof, candidate-staging bundle, Ledger promotion, four Scheduler stages, two
route decisions, two Core model invocations, and their result envelopes. The
statement filing must still derive the exact target advertised at admission:
same company, accession, filing hash, annual window, dimensionless diluted EPS,
and dimensionless diluted weighted-average shares. Cited passage offsets and
hashes are reverified through the configured document registry.

The full authority contains the exact cited text and provenance but no amount,
formula, or inferred fiscal period. `financial_note_evidence_binding` returns an
11-field immutable projection for a company structure definition. A caller can
later pass its stable `ref` to `resolve_financial_note_evidence_ref`; that call
rebuilds the authority from the underlying immutable records and must reproduce
the held projection exactly.

The resolver takes already-open Core, router, and candidate-staging SQLite
connections plus an already-configured `DocumentResearchRegistry`. It performs
no DDL and constructs no runtime, Scheduler, Router, or writable authority.
Its accounting check is deliberately labelled `promotion_checkpoint_only`: it
replays the formal results, route/invocation identities, result envelopes, and
self-hashed accounting proofs saved by the canonical promotion, but does not
claim a fresh budget-ledger replay. Canonical promotion is the insertion-time
gate that performs that full execution replay.

This first capability supports exact annual note-text applicability only. It
does not make quarterly evidence ready, derive a diluted-EPS numerator, extract
numeric adjustments, or infer note semantics from prose. Those remain work for
the structure validator and a later typed extraction contract.

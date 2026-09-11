# Configurable source-reading bounds — 2026-09-11

The extraction model configuration now accepts a closed `reading_limits` block:
`window_chars`, `quote_chars`, `max_document_chars`, and `max_pdf_pages`. Values
must be positive integers, quotes must fit the window, and the window must fit
the document ceiling. Omitted fields retain the legacy 12000/1200/600000/400
defaults. These are source-processing bounds; per-call tokens, dollars, and
timeouts remain separate effective budgets.

The configured character/page limits reach the verified AlphaEngine/public-web
source readers. Window and quote geometry reaches extraction, and explicit
reading configuration enters the context hash. Changing it therefore cannot
replay a result for different source windows. No source is silently truncated
under an old context identity. The web renderer still declares truncation;
whole-document completion must refuse such a rendering.

Cockpit no longer duplicates the 600000-character/12000-character offset rule.
The source service validates the exact offset against the installed geometry
and actual document length. Previous-page navigation consumes the returned
geometry. This makes windows beyond 600000 reachable after configuration.

Validation: 56 tests passed across reading-limit, extraction, public-web source,
fetched-filing, and review-control suites. A >600000-character fixture preserves
its final revenue-recognition passage after increasing the configured limit.
Service tests verify contiguous configured windows, changed context hashes,
and propagation into source receipt verification. No live configuration changed.

Next: include explicit long-document bounds in the reviewed deployment delta;
verify real annual source completion separately from process health. Output
schema item counts and acquisition byte/page contracts remain separate audit
items, not claims that every operating limit is now configurable.

Installation follow-up: model configuration regeneration now preserves validated explicit reading limits, in addition to transport/capacity and call/run budgets. Invalid limits refuse replacement. An installed-config round trip and the actual Cockpit/extraction budget consumers pass 18 tests; extraction uses its declared 64k/4096/$1/600-second defaults while explicit purpose overrides remain final.

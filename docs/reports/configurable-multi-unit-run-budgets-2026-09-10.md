# Configurable multi-unit run budgets — 2026-09-10

The dossier, industry-framework, and deep-insight runs now resolve their total
cost and unit bounds from the governed model configuration. An explicit CLI
`--max-units` remains the final override for dossier and framework runs.

Before each draft, the run retains room for both that producer call and the
required independent verifier. The final verifier check uses its own purpose's
effective call budget rather than the producer constant. Runs that cannot fund
the pair publish nothing.

Earnings preview and calibration pool admission now reserves the sum of the
effective writer and verifier purpose budgets for that specific window. A
cheap producer can no longer make an expensive verifier look affordable by
borrowing the producer's ceiling.

Validation: 165 focused tests passed across call-budget, dossier, industry
framework, deep-insight, and earnings-season suites. No live configuration,
model, or broker was touched.

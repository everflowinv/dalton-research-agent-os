# Foundation issuer-proof fixture regression — 2026-09-10

The numeric, metric-discovery, and automated qualitative extraction tests reused an earnings-call fixture that had no persisted search-metadata issuer proof. The production gate correctly returned `not_attributed`; changing it would allow a transcript returned by the wrong company's search to influence claims and metrics.

The shared harness now exposes an explicit `add_issuer_proof()` fixture operation. Tests that represent a genuine Accenture FY2026Q3 earnings call invoke it and persist both required facts: an issuer-position title and `named_companies=["Accenture"]`. Tests that exercise missing or wrong issuer metadata leave the proof absent or conflicting and continue to assert zero model calls.

Validation with Python 3.13:

```text
PYTHONPATH=src /opt/homebrew/bin/python3.13 -m unittest tests.test_document_extraction_automation.AutomationAdmissionTests tests.test_document_numeric_lane tests.test_metric_discovery_lane tests.test_admission_attribution tests.test_document_subject
```

Result: 60 tests passed. The entire `tests.test_document_extraction_automation` module still has one unrelated baseline failure in `WebAdmissionTests`: its public-web fetch child exits before this change on frozen `e9d06c2`; the identical isolated test fails in the untouched frozen acceptance tree. No production gate or runtime source was changed.

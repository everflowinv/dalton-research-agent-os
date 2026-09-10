# Standalone research HTML export

The new `python -m dalton_core.research_html_export` command reads the active mission and current research-library records through a SQLite read-only connection. It writes a self-contained HTML report and a deterministic JSON manifest containing the exact mission and product version/hash bindings.

The report begins with the mission and conclusion-bearing products, includes a table of contents, section prose, evidence refs, gaps, human memo approval state, quantitative tables, and inline SVG only when at least two hash-verified quantitative Claim versions expose finite values with the same typed metric, unit, scale, and currency. Missing data and unavailable charts remain visible. Publication is never presented as human approval.

Optional figures use a closed manifest with a local PNG/JPEG path, SHA-256, caption, and source refs. The exporter verifies the digest and file signature, embeds bytes as a data URI, and performs no external fetch. All authority prose and labels are HTML-escaped; the output contains no script or raw source HTML.

Example:

```sh
PYTHONPATH=src python3 -m dalton_core.research_html_export \
  --core-db /path/to/core.sqlite \
  --company-ref company:sec-cik:0001467373 \
  --output /tmp/acn-research.html
```

Validation: `PYTHONPATH=src python3 -m unittest tests.test_research_html_export` passed 5 tests, including a real read-only Core fixture, stable bytes, partial-data rendering, escaping, typed Claim resolution, incompatible-series refusal, and asset hash refusal. Playwright rendered the representative report at 1440×1000 and 390×844 with document width equal to viewport width; the quantitative table scrolls within its container on mobile. Chromium PDF generation completed. No live state, network source, signature, or model call was used.

The follow-up review removed all numeric parsing from prose. The exporter now holds `PRAGMA query_only=ON` and one explicit read transaction across mission, product, approval, and Claim resolution. Signed values use a real zero axis. Structured dossier sources and gaps are rendered through known textual fields rather than Python representations; product reasons/gaps and exact human decision ref/actor/time are visible.

Second review hardening binds chart series by subject, metric, basis, unit, scale, currency, period grain, and explicit actual/estimate classification; duplicate refs or periods are refused. Claim IDs must equal cited refs, superseded/rejected/retired versions are excluded, and labels display currency, unit, and scale. SQLite filenames containing `?` or `#` are covered through `Path.as_uri()`. User-supplied assets accept only namespaced source refs and are labelled as user-supplied.

Final authority-fixture validation exports a real published dossier and a real independently verified, human-approved Investment Memo through the public `export_research_html` entry point. The assertions bind the exported product refs and hashes, structured dossier source refs, and the exact Memo decision record, actor, and timestamp. Quantitative chart coverage now publishes Claims through the existing Store authority fixture rather than inserting test-shaped rows directly; retirement decisions and retracted adjudications are excluded. The combined focused suite passed 15 tests. The deliverable remains an authority-grounded HTML view of the records currently present in Core; it does not claim the editorial or analytical depth of a separately authored fund report.

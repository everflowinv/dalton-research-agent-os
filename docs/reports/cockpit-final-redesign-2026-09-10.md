# Cockpit final redesign — 2026-09-10

This change turns the Cockpit home page into a light Chinese research workspace. The mission and system state remain visible first; operational detail is collapsed. The company area is now a five-by-six journey matrix whose state comes only from `stage_ref` and `stage_status`. Selecting a company opens one focused dialog with overview, evidence, model, event, and product tabs.

The product tab reads the new read-only `/v1/cockpit/research?company=...` projection lazily and renders each authority product as available, missing, or invalid with its sections, sources, and gaps. It does not infer completion from counts. Investment Memo approval now exposes the full backend `memo_review`: all key questions, producer groups, formal checks, gaps, input bindings, and verified body hash before its human decision controls.

Existing goal/steering/Ask/approval POST paths and CSRF behavior are unchanged. Navigation uses inline SVG icons. The company dialog has ARIA dialog/tab semantics, arrow-key tab movement, Escape/backdrop close, visible focus, and restores focus to its company row even when polling rerenders the matrix.

## Verification

- `node --check /tmp/cockpit-final.js`: pass.
- `python3 -m py_compile scripts/qa_cockpit_final_redesign.py`: pass.
- GET-only fixture rejects POST with HTTP 405.
- Playwright desktop 1440×1000: mission, summary, collapsed operations, matrix, company dialog, and product tab rendered.
- Playwright mobile 390×844: document width exactly 390 px; journey matrix has its own 840 px horizontal scroll region, so the page does not overflow.
- Playwright keyboard: Escape closes the dialog and focus returns to `company:CTSH`; left/right tab controls expose correct ARIA selection.

Screenshots:

- `output/playwright/cockpit-final/desktop.png`
- `output/playwright/cockpit-final/company-detail.png`
- `output/playwright/cockpit-final/company-products.png`
- `output/playwright/cockpit-final/mobile.png`

The QA fixture contains representative synthetic content and performs no mutation, model call, signing, or external request. Backend integration of the research-product endpoint is delivered separately; this frontend fails visibly if that endpoint is unavailable.

## Follow-up acceptance fixes

The final pass moved the journey matrix directly below the mission, restored scale and source-grade rendering for financial figures, and normalized all retained legacy surfaces to the light palette. Every navigation item is now a native button. All readers share dialog setup and close behavior, trap Tab focus, and return focus on Escape.

A browser assertion waited for the research endpoint and confirmed the product body, structured `kind/ref/text` source (`claim · claim:v8 · 公司披露`), and explicit missing reason were rendered, with no `[object Object]` leak. A browser-rendered 12-question Memo fixture confirmed all 12 questions, unknown state, falsifier, formal evidence, group checks, gaps, and bindings were visible. The final desktop screenshot starts at scroll position zero with the journey matrix in the first viewport. The final mobile check reports document width 390 for a 390 viewport and the matrix begins at 656 px.

# Cockpit Research Library Authority Review

Date: 2026-09-10

## Scope

Reviewed the read-only research-library projection against the repository's real
CompanyDossier, DebateMap, IndustryFramework, MissionDeliverable, and
CoverageMission authorities. No live state was read or changed, and no HTTP or
model operation was performed.

## Correctness fixes

- Authority validation failures are isolated to the affected product. A corrupt
  dossier, debate map, or framework can no longer fail the whole library response.
- DebateMap sections retain the typed market position (`available`, `lean`) and
  analyst position (`state`, `side`) in addition to their prose and claim refs.
- Every product identifies its actual subject. The industry framework is bound to
  the mission industry; company products remain bound to the requested company.
- Memo publication and approval are separate. Approval is reported only when the
  folded current `investment_memo` stage points to a valid decision record whose
  evidence includes the exact memo version. A memo from another mission is marked
  historical, and a newer memo cannot inherit an older memo's decision.

## Validation

`PYTHONPATH=src python3 -m unittest tests.test_cockpit_research_library_authorities`

Result: 3 tests passed. The tests use real DebateMap and investment-memo authority
fixtures, including a real Store/Scheduler/Router memo publish-and-approve flow.

## Limits

This slice supplies the authority-backed projection only. HTTP wiring and visual
presentation remain owned by the integrating Cockpit work.

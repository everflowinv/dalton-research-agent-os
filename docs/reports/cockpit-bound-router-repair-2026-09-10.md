# Cockpit bound-router repair — 2026-09-10

The model page now resolves each purpose against the router database named by
that purpose's actual consumer configuration. Policy refs are scoped by
`(router path, policy ref)`, so identical refs in different databases cannot
share a cached policy or catalog. A missing database or policy marks only that
purpose unreadable; the remaining page stays available.

Resident service bindings use their real router fields. In particular, the
bounded planner reads `planner_model_router_db`; agenda and both thesis-impact
phases read `model_router_db`. The top-level service router remains the legacy
fallback.

A legacy policy with one allowed profile is displayed as a fixed model. A
legacy policy with several allowed profiles is displayed as an unordered
candidate set because the router applies policy preferences to those
candidates; their JSON order is not a fallback sequence. The UI uses commas
without numbered arrows for this case.

The page distinguishes readable from editable bindings. A dynamically injected
Cockpit config outside the registered state-directory files remains visible,
but its controls are disabled because the existing writer contract cannot
safely target an arbitrary browser-supplied path. Registered config files and
known resident service pins remain editable. No write-side path authority was
expanded.

Validation:

```text
PYTHONPATH=src python3 -m unittest tests.test_model_selection
Ran 81 tests in 1.278s — OK
```

The focused regressions cover two router databases containing the same policy
ref with different profile pins, the planner-only nested router field, an
unresolved purpose that does not hide healthy purposes, the unordered
multi-profile legacy candidate set, and read-only display of a dynamic external
binding.

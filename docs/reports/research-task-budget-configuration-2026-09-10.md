# ResearchTask configurable admission budgets — 2026-09-10

ResearchTask no longer clamps a configured admission batch to three. `research-task-lane.json` accepts a positive `max_admissions_per_tick`, a strict `retired_templates` list and optional `task_budget` fields `max_rounds`, `max_cost_units`, `max_seconds`. Omitted round/admission defaults come from packaged `run_budget_defaults.json`; explicit round limits can exceed four. Derived time and cost-unit budgets cover the selected rounds using the largest bound probe cost. Invalid settings fail visibly instead of silently restoring defaults.

The launcher reloads its configuration, includes effective task settings and the planner cost in its retry/ticket identity, and passes task bounds to the child. Admission, existing task reservation and the Cockpit task estimate read `bounded_planner.config.planner_call_budget.max_cost_usd` through the same driver configuration parser as execution (legacy cost is supported). Service planner edits retain their existing restart requirement. No live configuration or governance was changed.

Validation: 107 tests passed / 4.844s, covering research tasks, real SQLite admission, coordinator, registry, call budgets and budget configuration. New regressions admit five tasks with six rounds, carry explicit seven-round/time/unit limits through the child, reserve at the installed planner cost, rekey changed configuration and reject malformed configuration.

Next: activate only reviewed executable probe templates through the owner, address inquiry-specific probe selection and implement the missing search executors. A configured budget does not make these product gaps complete.

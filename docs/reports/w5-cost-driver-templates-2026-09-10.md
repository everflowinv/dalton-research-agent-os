# W5 cost-driver templates — 2026-09-10

## Result

The classification decision now selects a separate cost-driver checklist before a company model specification is accepted. Expense lines may bind to a cost slot allowed by that classification, or explicitly carry `cost_driver_slot: null` with a company-specific `cost_driver_unbound_reason`. A slot from another classification is refused. Omitting both fields remains the legacy wire and produces the same derived model-input and forecast-driver shapes.

The accepted binding travels through `company_model_inputs` into `model_forecast_driver` as advisory `cost_driver_slots`. It does not create or alter filed history, company actuals, economic-invariant roles, or forecast values. The dossier `supply_and_cost` unit uses the same cost registry as a deterministic checklist and records uncovered slots as gaps rather than inventing coverage or refusing the section.

## Frozen registry and contract

The immutable W4 demand registry and its published bytes are unchanged. W5 adds:

- registry ref: `cost-driver-template-registry:w5:v1`
- canonical artifact: `deploy/phase9/w5-cost-driver-template-v1.json`
- canonical content hash: `40f4b53330e4df5ce9fe88ee1d9dfbe70806fcf67df6ca8f1d0d6ba1bb056947`
- contract: `contracts/cost-driver-template-registry.schema.json`

The company-model-spec task hash now binds both the existing W4 demand registry hash and the new W5 cost registry hash. That makes the changed prompt/parser semantics a new task identity; no published template version is edited in place.

## Migration and compatibility

There is no database migration. The company model spec schema gains two optional expense-line fields. Old model-spec records, hashes, model-input rows, and forecast-driver records do not gain synthesized null keys. New records can carry an explicit cost binding. The registry contract describes a frozen constants artifact, so like `DriverTemplateRegistry` it has a content identity and no authority-record `id` or `created_at`.

No D1–D9 owner decision is granted or changed. The template yields proposed model-spec classifications and dossier gaps only.

## Verification

Focused command:

```text
PYTHONPATH=src python3 -m unittest tests.test_cost_driver_templates tests.test_driver_template tests.test_company_model_spec tests.test_company_model_inputs tests.test_model_forecast_driver tests.test_framework_by_classification_wiring tests.test_company_dossier_draft
```

Result: 185 tests passed in 2.062 seconds.

Contract command:

```text
PYTHONPATH=src python3 -m unittest tests.test_contracts
```

The first run exposed the new frozen registry's missing constants-artifact exemption; the contract test was updated to apply the same rule as the W4 registry. Final combined result: 195 tests passed in 2.005 seconds.

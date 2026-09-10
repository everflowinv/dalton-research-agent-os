# W5 cost-driver template cross-review

Date: 2026-09-10  
Baseline reviewed: `ac68efb` (`beb5334` implementation)  
Branch: `cost-template-cross-review`

## Finding and repair

The classification-to-spec validation was correct at initial parse time, but its evidence was then discarded. A published spec carried `cost_driver_slot` without the classification and registry identity that made the slot valid. Consequently a stale or altered published record could flow through model inputs and forecast construction without another cross-classification check.

New W5 specs that explicitly classify any expense line now carry additive `cost_driver_template` metadata: registry ref, registry hash, and selected classification. `build_model_inputs` validates that metadata and every bound/unbound row before reading filed series. It copies the metadata into the derived table, and `build_drivers` validates it again at the forecast boundary. A cross-class slot, stale registry identity, missing metadata, or null slot without its explicit company-specific reason is refused before forecast publication.

Legacy specs remain byte-shaped as before: when expense rows omit both W5 fields, no `cost_driver_template` key is synthesized in the spec or model-input table. The immutable W4 demand registry artifact and its canonical hash were not changed. Cost slots remain advisory metadata on drivers; filed cells, actual values, frozen economic roles, and forecast arithmetic are unchanged.

## Review result

The remaining W5 implementation passed review:

- classification selects only its own frozen cost-slot vocabulary, with generic fallback for refused or unsupported classification;
- the model prompt requires a bound slot or explicit company-specific unbound reason, and the parser enforces the null/reason pair while retaining the documented legacy wire;
- `supply_and_cost` dossier gaps use cost slots without inventing evidence or refusing the section;
- cost metadata never creates filed history or changes an actual value.

## Validation

Focused validation ran 198 tests successfully:

`PYTHONPATH=$PWD/src python3 -m unittest tests.test_cost_driver_templates tests.test_driver_template tests.test_company_model_spec tests.test_company_model_inputs tests.test_model_forecast_driver tests.test_framework_by_classification_wiring tests.test_company_dossier_draft tests.test_contracts`

New regressions verify classification/registry metadata publication, rejection of a cross-class published spec before actual-series reads, and rejection of altered metadata at forecast construction. Module compilation and `git diff --check` passed. Repository-wide validation remains owned by main integration.

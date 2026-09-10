# Extraction call-budget configuration — 2026-09-10

The qualitative document reader, numeric figure reader, and metric-discovery reader now resolve independent call budgets from the installed document-extraction model configuration. Their purpose keys are `document_extraction`, `document_numeric_extraction`, and `metric_discovery_extraction`; a purpose override cannot silently alter either sibling.

Each resolved budget controls the WorkOrder input, output, total-token, cost, and timeout fields. Broker transport uses the same resolved timeout, and reservation continues to derive from the configured WorkOrder cost ceiling and token bounds. Explicit budget configuration is fingerprinted into work identity and metadata, so changing a budget produces a new immutable order. A legacy config without either budget field preserves the old WorkOrder identity and metadata exactly, allowing already persisted work to replay.

Legacy defaults remain:

| Purpose | Input | Output | Cost | Timeout |
|---|---:|---:|---:|---:|
| `document_extraction` | 16,000 | 3,000 | $0.05 | 60s |
| `document_numeric_extraction` | 32,000 | 1,500 | $0.03 | 60s |
| `metric_discovery_extraction` | 16,000 | 1,200 | $0.03 | 60s |

Validation: `PYTHONPATH=src python3 -m unittest tests.test_call_budget tests.test_document_numeric_extraction tests.test_metric_discovery_extraction tests.test_document_extraction` passed 57 tests. No broker or model call was made.

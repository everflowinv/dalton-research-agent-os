# Company-model prompt clarification live fit

Commit `0fd2ec70` was checked against the live five-company Core snapshot through the same company-model state builder and prompt fitter used by the lane. Core was opened with SQLite URI `mode=ro`, `PRAGMA query_only=ON` and an explicit read transaction. Authority objects were attached to that existing connection without invoking their schema-installing constructors. No provider, lane, migration or authority write was invoked; the Core connection reported zero changes.

All complete prompts fit the configured 120,000-byte limit under task hash `adc99ada5bc838eabce5d75656fceb9c9e0430ffdb20ffb14b22099bd0533b36` and numeric policy `max_periods_per_series=8`, `max_total_cells=300`.

| Company | Prompt bytes | Included numeric cells | Omitted at prompt limit |
| --- | ---: | ---: | ---: |
| IBM | 119,955 | 242 | 58 |
| CTSH | 115,133 | 300 | 0 |
| EPAM | 119,800 | 243 | 57 |
| ACN | 119,856 | 271 | 29 |
| DXC | 119,966 | 269 | 31 |

The clarification causes the existing conflict-group fitter to omit four additional cells for IBM and EPAM, five for ACN, and four for DXC relative to the immediately preceding live prompts; CTSH still retains the full 300-cell selection. The result stays within the existing bounded-input contract and requires no budget or authority change.

The private receipt is `foundation-r16a-release/financial-note-live-input-audit/company-model-prompt-clarification-live-fit.json`.

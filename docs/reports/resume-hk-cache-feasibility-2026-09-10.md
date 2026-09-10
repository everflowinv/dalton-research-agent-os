# HKEX full-market buyback cache feasibility

Date: 2026-09-10  
Branch: `resume-hk-cache`  
Base: `d6c2d38`

F13 is feasible, but a complete implementation needs an explicit acquisition/view split rather than a local shortcut in the existing per-company child.

The HKEX `SRRPT{YYYYMMDD}.xls` file is one full-market document per trading day. Today `hkex_filings_cli` puts `hk_ticker` in the governed request parameters, canonical capture, artifact hash, and connector invocation identity, then filters the parsed grid for that ticker. Caching only the raw HTTP bytes can prevent a second network fetch, but it still truthfully produces two governed per-company invocations and two canonical captures. That is a useful partial optimization, not F13's promised “one invocation and one artifact.”

The complete seam should introduce a day-scoped full-market acquisition whose identity is `(operation, as_of, URL, governance version)` and whose single raw artifact is written once with atomic locking. Per-company derived views then reference that shared acquisition and artifact while retaining `hk_ticker` as their filtering parameter. This requires coordinated source/invocation contracts and an explicit governance identity decision, because silently dropping ticker from only the invocation hash would collapse two distinct governed requests while the approved input contract still requires ticker.

No code or connector contract was changed. The A-share reuse concept remains a follow-up after the HK acquisition/view contract is decided.

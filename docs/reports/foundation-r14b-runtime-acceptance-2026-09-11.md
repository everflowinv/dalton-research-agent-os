# R14b runtime acceptance and R14c follow-up — 2026-09-11

R14b is installed and published verified. This records runtime acceptance separately from research-product completion.

## Accepted release

- Source: `29aa03d96006a34ca6841e9b98bf77dcb93f38d2`.
- Complete suite: 7,474 tests, zero failures/errors, one skip; 737.764 seconds.
- Wheel: `617858a0ba34ba055c20cf025a026bb6a72504811cfadcf60d4e875e3dfef7b0`; 602 exact runtime files and three JavaScript checks.
- Current-state rehearsal: 14 steps, 42 ticks, zero escaped external calls.
- Separate clean operations source: `52360f4c349f2b275d97c2465e39f2bd4b045569`.
- Deployment: 19:29:20–19:30:46 UTC, exit 0. Controller PID 37259 started 19:30:12.768650 UTC.
- Sustained health: 45/45 samples over 670.946 seconds; same postdeployment controller, all required checks pass. Post-observation byte recheck passed; release pointer published verified.
- All 17 model configs, service and OpenClaw raw bytes, mission v14 and existing approved source authority preserved. Configuration, service and external mutations: 0 / 0 / 0.

Private evidence is in the owner activation directory under `foundation-r14b-release`; no private research corpus or configuration content is committed here.

| Evidence | SHA-256 |
| --- | --- |
| Manifest | `be6ccde7998b63ff1bf925cd0a15d78967b8751d937688dbf6fcfe9c64a57e44` |
| Deployment receipt | `bd61722306d0d4dcf9b025c7006ae995d1487d8c3f65eecec1b02db27aa93726` |
| Installed verification | `c42f437e6e8c3e001619bdf439f05c8474100b49140936b232c10e4117d92f3d` |
| Health summary | `23991f06fb8bb3575bb3f36f173b5b0ba61f46d68d905b7bc1f787c889840767` |
| Finalization | `60e58793015c3f41a2deb7a59a5dd0c6130226cd6bc8df3542fb3965cdb59d95` |
| Publication receipt | `5b37f1e8d7d5962e355f5ba17b12798fcd5a054ecc37a55aee02a532c9b61323` |

## Actual product observation

The read-only 19:37 UTC snapshot found five new source-discovery registrations from earlier Gemini calls: 50 result references, including 18 newly discovered documents. The ACN registration binds the original invocation `connector-invocation:gemini-web-search:5a9e984641768cccf529` and unchanged original envelope. This is recovery from existing completed results, not another search. The audit hash is `54fd35525f824155869ad1d36121f75a8f490a1ab0cbf8260c93fec66d33dc57`.

The planner produced formal plans after deployment. The directed-question producer now runs, but there were zero document admissions at the snapshot. Its summaries identify `company_ref is outside the exact MandateVersion scope` for selected EPAM/DXC inquiries. The active Mandate explicitly lists the industry and ACN; the signed Mission lists all five companies under that industry. The legacy backlog membership check does not understand that exact mission binding.

## Reviewed follow-up, not yet deployed

R14c is frozen at `ec0d9a17a02db338fec19adc1bd66ac2b02d7c75` while its full suite and rehearsal run:

1. Bind directed questions to the exact active Mission ref/hash, company universe, automation principal, question grant, connected source and active industry Mandate. Validate archived planner/formal/registration origin before writing the question; revalidate inside the transaction. Default unbound backlog scope remains exact. Constructor/schema initialization stays outside BEGIN; fault injection proves no partial question writes. Root 76 tests and independent 71 tests passed.
2. Dismiss only proven unreadable document renderings without issuing read-completion or Claim receipts. Missing download tickets remain recoverable; nonzero offset errors remain recoverable. Unchanged transient queues use the existing hold and reopen on tracked queue/config changes or expiry. Final extraction suite: 122 passed.
3. Align conviction prompts and parser bounds; shared market direction can still contain supported magnitude/timing/probability/valuation differences. Content refusals hold until evidence/contract changes. Parser bounds run before verifier spending. Final focused conviction suite: 159 passed. This does not replace the remaining higher-level judgement policy.

## Excel and annual models

The Desktop AMZN reference remains the strict export acceptance target. The first actual rendered integration is not accepted: period headings overlap, Driver metrics repeat per period rather than across columns, Financials section fill differs, and Valuation retains a generic technical table instead of the source annual metric blocks. The author is correcting these against the original renders. Internal annual projection persistence is also under review so Excel consumes the same authoritative model values.

Next: finish R14c exact-release acceptance, deploy under existing owner authorization, then verify actual document admission/read results. Complete annual financial authority and rendered/recalculated workbook parity in the separate R15 line. Keep human source signatures and research checkpoints independent.

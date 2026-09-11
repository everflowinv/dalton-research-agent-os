# Extraction token-budget boundary — 2026-09-11

The extraction WorkOrder historically defaulted to 16,000 conservative input
tokens, 3,000 output tokens, a 0.05 USD call ceiling, and 60 seconds.  The live
configuration did not override that call budget.  The router and worker both
count UTF-8 bytes as the conservative token bound.

Actual annual-report prompts required up to 16,606 bytes for ACN, 16,505 for
CTSH, and 16,629 for DXC.  Some calls therefore failed routing before any model
invocation.  One bound example required 16,038 input and 19,038 total against
16,000 and 19,000 respectively.

Work construction now builds the canonical prompt once and applies the same
UTF-8 counter before enqueue.  A prompt above the configured input ceiling is
rejected with both exact counts.  The source window is never silently truncated
under its existing context hash.  The configured input, output, cost, and
timeout values remain frozen into WorkOrder identity and budget.

For the observed English annual filings, a reviewed configuration of 18,000
input tokens covers the measured 16,629-byte maximum with 1,371 bytes of margin.
Keeping 3,000 output tokens gives a 21,000 total ceiling.  A 120-second timeout
is a conservative operational proposal now that longer extraction calls are
explicitly authorized; it does not increase call count or cost.  These values
must be installed explicitly through the existing `purpose_call_budgets` entry
for `document_extraction`; this code does not silently change the owner ceiling.

Tests cover exact boundary acceptance, one-byte-over refusal, configured long
timeout propagation, and a multibyte Chinese/emoji prompt using the same UTF-8
counter.  Ninety related extraction, setup, preflight, admission, and automation
tests passed without a network or model call.

# Event model budget against the live catalog — 2026-09-10

## Scope and method

This was a read-only admission review. I opened the live model-router database
read-only, copied it with SQLite's backup API to a temporary directory, and ran
real `ModelRouter.route` decisions against that copy. No broker transport or
model was called. No live file or database was changed, and no credential value
was read or printed.

The original check used the then-proposed 5,000-byte / 700-token / $0.10
contract. That contract is historical: the owner subsequently authorized a
$1 per-call ceiling so the event lane can retain its full evidence context.

The current check used the replacement contract exactly: 60,000 maximum input
bytes (conservatively presented to the router as 60,000 input tokens), 1,500
output tokens, 180 seconds, and the authorized $1 per-call ceiling. It used the routing policies
actually referenced by the installed `event-judgement-model-config.json` and
`event-verifier-model-config.json`.

## Live policy and catalog result

The installed producer policy selected `openai/gpt-6-astra`, family
`openai-gpt-6`. Its current public rate card is $10/M input and $50/M output,
giving a worst-case reservation of **$0.675000**. The other configured brain
fallback, `claude-cli-gateway/claude-fable-5-1`, has the same public rates and
the same **$0.675000** reservation. Both are below the authorized $1 cap.

The installed verifier policy has three live, priced links:

| Profile | Family | Maximum reservation | Result |
| --- | --- | ---: | --- |
| `profile:claude-fable-5-1` | `anthropic-claude-5` | $0.675000 | admitted unless producer is Anthropic |
| `profile:zai-glm-5-3` | `zhipu-glm-5.3` | $0.090600 | admitted unless producer is Zhipu |
| `profile:gemini-3-5-flash-lite` | `google-gemini-3` | $0.021750 | admitted unless producer is Google |

Real routing on the temporary copy produced these decisions:

- OpenAI producer → Claude verifier at $0.675000.
- Anthropic producer → Claude was excluded and ZAI was selected at $0.090600.
- Zhipu producer → ZAI was excluded and Claude was selected at $0.675000.
- Google producer → Gemini was excluded and Claude was selected at $0.675000.
- An unclassified producer family → routing was rejected before selection.

Thus every family that can currently produce through the installed brain policy
has at least one independent verifier route inside the cap. The active producer
and verifier chains contain no unpriced profile. Unpriced profiles elsewhere in
the catalog cannot enter these calls because both installed policies restrict
their allowed profile IDs; this review did not weaken that restriction.

## Boundary

This establishes admission against the public catalog and installed policy
snapshot read on 2026-09-10. It does not establish broker availability,
credentials, output quality, or that a bounded prompt retains enough evidence;
those are separate runtime and prompt-contract checks. In particular, the
prompt preservation blockers identified in the companion code review must be
fixed before this lower reservation is safe to release.

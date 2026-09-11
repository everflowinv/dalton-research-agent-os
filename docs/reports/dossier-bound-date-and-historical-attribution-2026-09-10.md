# Dossier sourced dates and historical attribution

The live ACN draft's complete English date was treated as an unsourced day
number even though its cited material bound the exact ISO period. The number
source checker now recognizes equivalent complete English dates only when the
same date exists in supplied source-period material. An unbound date, a different
period, or a separate unsupported quantity remains a refusal.

The EPAM historical price-drivers section cited a report attributing a stock
move to an analyst price-target reduction. A substring check misclassified this
third-party event as Dalton's own investment conclusion. The conclusion checker
now excludes only an explicit historical target-change phrase supported by a
Claim cited by that very sentence. Source attribution must have a direct
actor/action phrase or a target change explicitly from/by the third-party actor,
with matching direction. Proximity alone, a generic broker downgrade, a negated
source, or another section cannot qualify. Author recommendations, price targets,
valuation conclusions, and future predictions remain refused. Original section
content is never edited by the checker.

Both deterministic contracts enter Dossier failure/ticket identity, leaving
producer prompt identity and successful published version hashes unchanged.
Existing failed summaries and holds are retained; a corrected contract can be
admitted without deleting them or repeating already successful model calls.

Validation: 43 Dossier authority tests, including cited historical attribution,
wrong or unrelated refs, opposite actions, negation, nearby management actions,
and retained author-conclusion refusals. Actual EPAM fourth-run block replay
passes without model calls or live writes; private final replay receipt SHA-256
`8ed3549be8525fa863f85ea2e6944251a80f9c0bf115f02550264ad1a87dee68`.
The source-bound ACN date replay receipt SHA-256 is
`6584c073f65b3e68b22ea2d51d4d13793dd9890d1a0983f0c1acc8d55f319457`.

# Truthful dossier and event run results

Two child entry points reported `status: succeeded` after every attempted formal output had been refused. This made an ACN dossier with `dossier_status: verification_failed` and zero published versions, and an event batch with eight refused effects and zero judgements, look operationally successful to launchers and Cockpit.

A dossier now reports top-level `failed` when every attempted draft violates its contract or when the independent verifier does not pass the assembled draft. Its bounded `failure_reason` carries up to three draft refusal reasons or the verifier reason/findings. A run stopped solely by its configured run-cost bound retains the prior `unverified` completion semantics; no-input remains `idle`; and a partial draft that passes all publication gates remains a success.

An event batch now reports top-level `failed` when it attempted work, recorded refusals, and produced no formal judgement. Its reason includes up to three refusal reasons and is capped at 500 characters. Mixed batches remain successful and say `judgement_status: partial`; no unjudged events remains idle. Spend recording is unchanged and happens before outcome aggregation, so rejected producer/verifier calls remain charged exactly once. Configuration fingerprints and WorkOrder identities are unchanged: the status correction does not manufacture a new paid retry for a cached contract refusal; the lane failure ledger retains its bounded retry/hold behavior until configuration changes rekey the batch.

Focused validation: 119 dossier, event, configuration-retry, and Cockpit tests passed with one existing skip. The dossier suite then passed 59 tests including all-refused, mixed publish, verifier rejection, run-budget gating, and no-input behavior.

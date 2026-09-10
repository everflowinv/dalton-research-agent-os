# Zero-base review F16/F17 fixes

Date: 2026-09-10  
Branch: `resume-zero-base-fixes`  
Base: `w4-zero-base-review` at `a208d04`

## Result

F16 changes the recurring trigger from calendar-month membership to at least 30 elapsed days since the company's latest review. A previously unseen `earnings_calibration` remains immediately due; recording that earnings-triggered review becomes the new 30-day anchor. January 31 followed by February 1 therefore produces only one review.

F17 adds a mandatory verifier call before `ZeroBaseReviewAuthority.record()`. The verifier prompt receives the four answers, the as-of date, and only archived thesis, debate, and claim references. Its output is closed to `verdict` and `findings`; malformed output, rejection, an unresolved model family, or a verifier from the producer's family returns `refused` and publishes no review. A successful review stores verifier provenance and the `model_family_ne` evidence in its append-only record.

Deployment wiring now treats producer and verifier as a pair across the CLI, child launcher, lane arguments, plist fragment, model-purpose tier map, `install.sh`, and rehearsal lane-switch inventory. The installer refuses a visibly identical producer/verifier pin.

## Scope held

D8 is not implemented: zero-base reviews are not added to `MissionDeliverableAuthority`, and the outcome window remains unchanged.

## Validation

Focused command:

```
PYTHONPATH=$PWD/src python3 -m unittest tests.test_zero_base_review tests.test_mission_zero_base_lane tests.test_purpose_tiers_cover_every_registered_purpose tests.test_rehearse_deploy tests.test_judgement_outcome_reflection
```

Result:

```
Ran 142 tests in 0.301s

OK
```

Full-suite result will be appended after `python3 -m unittest discover -s tests -t .` completes.

# Investment Memo verifier provider schema

The Investment Memo verifier now uses a packaged, purpose-bound provider output schema. The closed contract requires a pass/reject verdict, the exact canonical memo body hash, and a bounded set of known finding codes. The adapter refuses unknown or cross-purpose contracts before transport.

The packaged schema reference and canonical hash participate in the model work identity. The memo child ticket fingerprint also includes the effective provider contract, so a contract correction can retry the same business input without weakening normal restart idempotency.

Focused validation covers the real adapter structured-output request boundary, schema vocabulary against the Memo validator, launcher fingerprinting, packaging, and existing Memo draft/lane behavior. No transport or model call was made.

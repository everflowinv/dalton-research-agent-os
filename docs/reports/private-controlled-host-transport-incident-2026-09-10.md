# Controlled Google host transport incident — private evidence

At approximately 16:59 America/New_York, a diagnostic intended to replace the
provider dependency with a local stub reached the managed Google transport
because that transport captures its own guarded fetch implementation rather
than reading `globalThis.fetch`. It sent one minimal request containing only
`Return JSON.` to `google/gemini-3.8-flash`. The provider returned HTTP 200;
OpenClaw discarded the result because no provider-control proof existed.

No Dalton authority, scheduler, broker journal, configuration, or deployment
state was written. Provider usage and cost were not returned, so both remain
unknown. No credential, request header, response body, or key is recorded here.
The diagnostic was not repeated.

The observation identified the defect: model preparation selected OpenClaw's
pre-bound Google stream transport. That transport performs generation but does
not implement the installed `countTokens`, structured-output, token/cost
admission, and proof contract in the native `@openclaw/ai` transport.

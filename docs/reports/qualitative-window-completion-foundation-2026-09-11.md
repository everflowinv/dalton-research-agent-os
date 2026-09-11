# Qualitative window completion foundation — 2026-09-11

The document extraction child previously treated every enumerated source
window as a completed qualitative read even when its formal model result was
failed.  When the last source offset was reached, the review entered admission
with no suggestions and was automatically dismissed as containing no
admissible qualitative statement.

That conclusion was not supported.  A failed model execution establishes
nothing about the source.  The child now marks a review complete only when all
of its qualitative windows have successful persisted results.  A failed or
pending window leaves the review open.  Failed entries also retain the bounded
error code and WorkOrder reference already validated by the extraction service,
so an operator can distinguish source conclusions from execution failures
without exposing prompts or responses.

This does not weaken qualitative validation.  A document whose every window
successfully returns a valid empty suggestion set may still be dismissed.  A
mixed document with one successful empty window and one failed window remains
open, as does an all-failed document.  Existing human review decisions are not
changed, and this patch does not reopen any historical review.

The regression suite exercises all-failed, mixed, and all-valid-empty documents
through the extraction child and formal review resolution path.  It makes no
network or model calls and performs no live-state writes.

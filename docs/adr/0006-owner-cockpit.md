# ADR-0006: The owner's cockpit is five places, and none of them speaks machine

*2026-09-07*  ·  Status: accepted  ·  Supersedes the cockpit surface of ADR-0002 and ADR-0003 B for the owner; those planes remain for operators at `/legacy`.

## Context

The cockpit grew one review pane at a time: documents awaiting extraction, transcript packets, candidate Claims, a natural-language composer that produced typed intent candidates, an answer router that only recognised registered questions, and an Agenda feedback list. Every pane showed refs, hashes and state names, and most of them asked the owner to do work ADR-0005 had already handed to automation.

The owner said what they need: one place to set the overall research goal and see the sub-tasks the system derived from it with progress; one place to steer; one page that shows what the system is doing; one page for ad-hoc questions; one page for the things that genuinely need a person. Clear, no machine language.

## Decision

**Five views, one shell.** The Tailscale-identified, session-and-CSRF shell (`agenda_control`) stays; it serves a rebuilt page at `/` and a new `cockpit_plane` behind `/v1/cockpit/*`. The old page and its routes remain at `/legacy` for operators and for the tests that pin their contracts.

**Goal = the active CoverageMission.** The goal view is the mission's title, objective, research questions, deliverables and sources, in the owner's words. The "sub-tasks the system decomposed" are what the mission already implies: one line per company (collect, read, keep tracking) with counts from the Ledger and the mission tables, and one lane per connected source. Progress is counted, not estimated.

**Setting a goal publishes a mission version, after confirmation.** The owner writes a goal in prose; a bounded model call drafts a title, objective, research questions and sub-tasks; the draft is shown back and held as an open item; only the owner's confirmation publishes it, as a new mission version derived from the current one with the coverage list, bindings, autonomy and budget unchanged. The draft is bound to the mission version it was written against and is refused if the mission moved.

**Steering is the same mechanism with a narrower lever.** A steering sentence becomes a proposed change to the research questions (add, remove) and, when the sentence changes the goal itself, a reworded objective; anything the lever cannot do is listed as such. Confirmation publishes a mission version. To make steering real rather than ceremonial, the mission's objective and research questions now enter the extraction prompt through the drafting context, so a new mission version re-keys every window and the next drafts follow the new questions.

**The log is assembled, not journaled.** The research log reads the lane tickets on disk, the heartbeat, and the Ledger's recent Claims and closed reviews, and renders each as one plain sentence with a lane label and a company. Owner actions (questions, goals, steers, decisions) are recorded in a cockpit journal and appear in the same stream. Nothing new is written to the Core for the sake of the log.

**Questions are answered from Claims only.** An ad-hoc question selects the formal Claims (all of them, or the mentioned companies' first), hands them to a bounded model call with the theses and the goal, and returns an answer that cites Claim tags, a confidence and the gaps. The cited Claims are shown under the answer. The answer is a cockpit artifact; it is never a Claim and never enters the Ledger.

**Cockpit model calls are budgeted like extraction.** Ask, goal and steer use the extraction model configuration: same routing policy, same broker, and an admission in the shared day ledger bound to the active mission's daily caps, so a question is one of the mission's paid calls and the ledger fails closed when the day is spent. Each call is a scheduler WorkOrder, so a repeated request replays. The control process still holds no Core write handle.

**Approvals are the mission's human checkpoints.** The approvals view lists every undecided thesis admission, capability promotion, planner proposal on a live loop and forecast overturn candidate, plus the owner's own open drafts, each with a plain title and the decision the owner can make. Decisions go through the existing governance ops as the owner's Tailscale-derived principal; the cockpit adds no new authority.

## Consequences

- The owner never sees a document queue, a hash or a ref. Operators can, at `/legacy`.
- Changing the coverage list, connecting a source, raising a budget or adding a tool remain outside the cockpit's levers; the steer draft says so explicitly. Each needs its own authority change and will get its own slice.
- The live control config gains a `cockpit` section written by `cockpit_setup`; the installer runs it. Without it the page reports that the cockpit is not configured, and the legacy page still works.
- The intent plane (ADR-0002) is no longer the owner's steering surface; its typed candidates were consumed only by bounded planner loops, of which none is active. Its routes stay for the tests and for operators.

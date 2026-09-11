# Research reader product state — 2026-09-11

R5 read-only exports reproduced a misleading presentation: ACN had a published
incremental dossier with only three drafted units, while the reader showed only
`available` and a current mission binding. The Cockpit also omitted the memo
approval and mission-binding fields already returned by its API.

The library now exposes drafted-unit completeness separately from readable
publication status. Both HTML and Cockpit show partial versus all-units-drafted
and explicitly avoid claiming data freshness or quality acceptance. Classification
uses its actual typed source-bound sentences; its closed schema has no `status`
field. An all-unknown classification remains unavailable, while a sourced finding
of insufficient evidence can count as drafted. Existing authority bytes are not
rewritten. The HTML manifest carries the same completeness projection.

Cockpit now shows current versus historical mission binding and exact memo
approval state, including pending, rejected, historical, and accepted decisions.
Independent review found a read-side integrity gap: a hash-valid stage JSON was
not compared with its database identity columns. The reader now validates the
closed stage record, matches its ID, mission, company, stage, status, actor and
timestamp to the selected row, and requires the exact current mission hash and
human actor before displaying approval. A decision citing another memo remains
pending and explains why.

Validation: 23 library/HTML/download authority tests passed in 1.093 seconds.
The new tamper cases retain a valid JSON hash but mismatch company, mission, stage,
status, actor or record ID; each is displayed as invalid. A real authority test
covers one drafted unit plus placeholders and a fully drafted twelve-unit file
under SQLite query-only mode. Thirteen Playwright checks passed at 390 × 844,
including the new state branches, structured sources, missing reasons, tab/focus
behavior and document overflow. Browser log SHA-256:
`59e53eeb6d86acc9beea4b520ac23fcb7bb5fbdedaf12667978cd82d5f50ea19`.
The browser used isolated loopback fixtures, with no live writes.

The actual ACN candidate HTML exported from read-only live authority reports 3/12
drafted units. Its SHA-256 is
`f0835ea3a1006ff8fcfbf08fe3f1bd3163a0e655460ee6ea82c4f8d65742c662`.
These reader changes await the next release; they do not complete the dossier.

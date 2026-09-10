# Yfinance governance recovery

The market-price, consensus-estimate, and catalyst-calendar launchers now load and validate their existing ConnectorGovernance records before spawning a child. A proposed record returns `not_permitted` through the existing lane failure classifier without starting a process. The launch ticket binds the validated governance ref and hash.

Ticket identity now includes the exact governance hash. Consensus identity additionally includes both fiscal-year-end and last-reported-period-end, alongside company, ticker, and day. A same-day owner replacement of a proposed record with an approved record therefore creates a different governed request rather than reopening the failed ticket.

The three coordinators key failure state by company and governance hash when the real launcher exposes that identity. Settlement uses the hash captured in the ticket, so an old child's result cannot be attributed to a newly approved record. Legacy/fake launchers retain the prior company key for compatibility; no unrelated source or content failures are cleared.

Validation: 105 focused launcher and coordinator tests passed. The new tests use packaged ConnectorGovernance validation with proposed and approved records and do not invoke the network.

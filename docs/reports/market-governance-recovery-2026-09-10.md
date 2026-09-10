# Yfinance governance recovery

The market-price, consensus-estimate, and catalyst-calendar launchers now load and validate their existing ConnectorGovernance records before spawning a child. A proposed record returns `not_permitted` through the existing lane failure classifier without starting a process. The launch ticket binds the validated governance ref and hash.

Ticket identity now includes the exact governance hash. Consensus identity additionally includes both fiscal-year-end and last-reported-period-end, alongside company, ticker, and day. A same-day owner replacement of a proposed record with an approved record therefore creates a different governed request rather than reopening the failed ticket.

The three coordinators use a governance-hash key only for a preflight permission refusal. Real child failures continue to use the stable company business key, so a governance replacement cannot bypass a network, content, quota, or retry hold. Legacy tickets settle against that same stable business key rather than borrowing the current governance hash. A newly approved record has a new permission key and can launch without clearing any unrelated failure.

Validation: 105 focused launcher and coordinator tests passed. The new tests use packaged ConnectorGovernance validation with proposed and approved records and do not invoke the network.

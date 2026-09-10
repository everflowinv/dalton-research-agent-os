# Connector capacity recovery review — 2026-09-10

Reviewed `d10fb408c090f49a86a6a8bf932d604ff24c16ea` and added the
pre-journal retry correction in this branch.

The shared authority now recovers a reservation from a database containing
multiple provider accounts by binding the row to its exact workspace and
historical policy before mutation. Tests cover both manifest orders and both
the reserved and dispatched crash states.

An admitted runner request could previously replay a locally released quota
reservation forever. This occurs when shared capacity refuses after the local
reservation is written but before the runner journal records `reserved`. The
executor now detects that the local reservation is already settled and creates
the next authority-assigned physical attempt. If the matching shared
reservation itself ended before the journal write, one retry releases the old
local reservation and advances to the new physical attempt without reusing an
expired lease.

Credential-free public HTTPS transports are outside paid provider-account
capacity. AlphaEngine's actual search and document acquisition paths enter the
central `ConnectorTransportExecutor`; the bounded probe adds its separately
configured trailing-window cap and does not directly call the provider.

Validation:

`PYTHONPATH=src python3 -m unittest tests.test_shared_connector_capacity tests.test_connector_transport_executor tests.test_live_mcp_connector tests.test_mcp_managed_shadow tests.test_recorded_reference_shadow`

Result: 77 tests passed.

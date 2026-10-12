"""The generated Python defaults and packaged broker declare one capability."""
import json
import re
import unittest
from pathlib import Path
from dalton_core.openclaw_broker_limits import MAX_COMPLETION_TIMEOUT_MS
from dalton_core.openclaw_allow_patch import DEFAULT_TIMEOUT_MS
from dalton_core.openclaw_provider_catalog_sync import AUTO_PROFILE_TIMEOUT_MS
from dalton_core.call_budget import validate_budget_overrides


class BrokerTimeoutContractTests(unittest.TestCase):
    def test_python_profile_defaults_match_packaged_js_and_schema(self):
        root=Path(__file__).resolve().parents[1]/'integrations/openclaw-model-broker'
        schema=json.loads((root/'openclaw.plugin.json').read_bytes())
        source=(root/'src/protocol.mjs').read_text()
        bound=int(re.search(r'MAX_COMPLETION_TIMEOUT_MS = ([\d_]+);',source).group(1).replace('_',''))
        self.assertEqual(bound,MAX_COMPLETION_TIMEOUT_MS)
        self.assertEqual(schema['configSchema']['properties']['profiles']['items']['properties']['timeoutMs']['maximum'],bound)
        self.assertEqual(DEFAULT_TIMEOUT_MS,bound)
        self.assertEqual(AUTO_PROFILE_TIMEOUT_MS,bound)

    def test_capability_upgrade_does_not_change_owner_call_budget(self):
        for seconds in (120,900,1800):
            supplied={'timeout_seconds':seconds,'max_cost_usd':14,'max_output_tokens':128000}
            self.assertEqual(validate_budget_overrides(supplied),supplied)

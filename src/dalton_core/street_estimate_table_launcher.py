"""One child at a time for the deterministic scanner's held-table gaps."""
from pathlib import Path
from typing import Any
from .lane_child_launcher import LaneChildLauncher
from .store import content_hash
from .street_estimate_table_model import MODEL_CONFIG, VERIFIER_CONFIG


class StreetEstimateTableLauncher(LaneChildLauncher):
    TICKET_PREFIX = "street-estimate-table-run"
    TICKETS_DIRNAME = "street-estimate-table-runs"
    CHILD_MODULE = "dalton_core.street_estimate_table_cli"

    @property
    def configured(self) -> bool:
        return all((self.state_dir / name).is_file() for name in (MODEL_CONFIG, VERIFIER_CONFIG))

    def _command(self, *, ticket_dir: Path, company_ref: str, document_ref: str,
                 expected_request_hash: str) -> list[str]:
        return [self.python_executable, "-m", self.CHILD_MODULE, "--state-dir", str(self.state_dir),
                "--company-ref", company_ref, "--document-ref", document_ref,
                "--expected-request-hash", expected_request_hash, "--summary-dir", str(ticket_dir)]

    def configuration_identity(self) -> str:
        from .street_estimate_table_model import bounded_model_config
        import json
        return content_hash([bounded_model_config(json.loads((self.state_dir / name).read_text()), purpose)[1]
            for name, purpose in ((MODEL_CONFIG, "street_estimate"), (VERIFIER_CONFIG, "street_estimate_verifier"))])

    def start(self, *, request: Any, day: str, mission_version_ref: str) -> dict[str, Any]:
        from .document_extraction import validate_model_config
        import json
        configs = [validate_model_config(json.loads((self.state_dir / name).read_text()))
                   for name in (MODEL_CONFIG, VERIFIER_CONFIG)]
        expected = content_hash(request)
        digest = content_hash({"request": expected, "configs": configs,
                               "effective_config_identity": self.configuration_identity(), "day": day,
                               "mission_version_ref": mission_version_ref})[:24]
        return self.spawn(digest=digest,
            record={"company_ref": request["company_ref"], "document_ref": request["document_ref"],
                    "request_hash": expected, "day": day, "mission_version_ref": mission_version_ref},
            company_ref=request["company_ref"], document_ref=request["document_ref"],
            expected_request_hash=expected)

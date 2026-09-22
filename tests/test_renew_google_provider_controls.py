from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path

from scripts.renew_google_provider_controls import (
    EXPECTED,
    MODE,
    RenewalError,
    main,
    renewed_config,
)
from tests.test_openclaw_catalog_reconcile import _config


def controlled_config() -> dict:
    config = _config()
    profiles = config["plugins"]["entries"]["dalton-openclaw-model-broker"][
        "config"
    ]["profiles"]
    by_id = {profile["id"]: profile for profile in profiles}
    for profile_id, expected in EXPECTED.items():
        controls = {
            "mode": MODE,
            "rateCard": {
                "model": expected["model"],
                "serviceTier": "default",
                **expected["rates"],
                "verifiedAt": "2026-08-22T00:00:00Z",
                "expiresAt": "2026-09-22T00:00:00Z",
            },
        }
        if "thinkingLevel" in expected:
            controls["thinkingLevel"] = expected["thinkingLevel"]
        by_id[profile_id]["providerControls"] = controls
    return config


class ProviderControlRenewalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.verified = datetime.now(timezone.utc).replace(microsecond=0)
        self.expires = self.verified + timedelta(days=30)

    def test_only_the_two_date_fields_change(self) -> None:
        before = controlled_config()
        after, changes = renewed_config(
            before, verified_at=self.verified, expires_at=self.expires
        )
        self.assertEqual(len(changes), 2)
        for profile in after["plugins"]["entries"][
            "dalton-openclaw-model-broker"
        ]["config"]["profiles"]:
            old = next(item for item in before["plugins"]["entries"][
                "dalton-openclaw-model-broker"
            ]["config"]["profiles"] if item["id"] == profile["id"])
            if profile["id"] not in EXPECTED:
                self.assertEqual(profile, old)
                continue
            new_controls = copy.deepcopy(profile["providerControls"])
            old_controls = copy.deepcopy(old["providerControls"])
            new_rate = new_controls.pop("rateCard")
            old_rate = old_controls.pop("rateCard")
            self.assertEqual(new_controls, old_controls)
            new_rate.pop("verifiedAt")
            new_rate.pop("expiresAt")
            old_rate.pop("verifiedAt")
            old_rate.pop("expiresAt")
            self.assertEqual(new_rate, old_rate)

    def test_a_rate_change_is_refused_instead_of_blessed(self) -> None:
        config = controlled_config()
        profile = next(
            item for item in config["plugins"]["entries"][
                "dalton-openclaw-model-broker"
            ]["config"]["profiles"]
            if item["id"] == "profile:gemini-3-8-flash"
        )
        profile["providerControls"]["rateCard"]["outputUsdPerMillion"] = "3.75"
        with self.assertRaisesRegex(RenewalError, "re-audit"):
            renewed_config(
                config, verified_at=self.verified, expires_at=self.expires
            )

    def test_duplicate_controlled_profile_and_expired_renewal_are_refused(self) -> None:
        config = controlled_config()
        profiles = config["plugins"]["entries"][
            "dalton-openclaw-model-broker"
        ]["config"]["profiles"]
        controlled = next(profile for profile in profiles
                          if profile["id"] == "profile:gemini-3-8-flash")
        profiles.append(copy.deepcopy(controlled))
        with self.assertRaisesRegex(RenewalError, "must be unique"):
            renewed_config(
                config, verified_at=self.verified, expires_at=self.expires
            )
        with self.assertRaisesRegex(RenewalError, "still be in the future"):
            renewed_config(
                controlled_config(),
                verified_at=self.verified - timedelta(days=10),
                expires_at=self.verified - timedelta(days=1),
            )

    def test_apply_requires_reviewed_hash_and_keeps_a_backup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "openclaw.json"
            path.write_text(json.dumps(controlled_config()), encoding="utf-8")
            raw = path.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            dates = [
                "--verified-at", self.verified.isoformat(),
                "--expires-at", self.expires.isoformat(),
            ]
            with self.assertRaisesRegex(RenewalError, "requires"):
                main(["--openclaw-config", str(path), *dates, "--apply"])
            out = StringIO()
            with redirect_stdout(out):
                self.assertEqual(main([
                    "--openclaw-config", str(path), *dates,
                    "--expected-config-sha256", digest, "--apply",
                ]), 0)
            report = json.loads(out.getvalue())
            self.assertEqual(report["status"], "applied")
            self.assertEqual(Path(report["backup_path"]).read_bytes(), raw)
            self.assertNotEqual(path.read_bytes(), raw)


if __name__ == "__main__":
    unittest.main()

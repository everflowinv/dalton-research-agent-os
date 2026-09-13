from pathlib import Path
import unittest


class CockpitOverviewSingleFlightTests(unittest.TestCase):
    page = (Path(__file__).resolve().parents[1] / "src" / "dalton_core" /
            "cockpit_control.html")
    check = (Path(__file__).resolve().parents[1] / "scripts" /
             "check_cockpit_overview_singleflight.js")

    def test_overview_loader_coalesces_and_always_unlocks(self):
        source = self.page.read_text(encoding="utf-8")
        self.assertIn("if(overviewLoad)return overviewLoad", source)
        self.assertIn("finally{overviewLoad=null}", source)
        check = self.check.read_text(encoding="utf-8")
        self.assertIn("oneWhilePending", check)
        self.assertIn("retriesAfterSuccess", check)
        self.assertIn("retriesAfterFailure", check)
        self.assertIn("writesToLive: 0", check)


if __name__ == "__main__":
    unittest.main()

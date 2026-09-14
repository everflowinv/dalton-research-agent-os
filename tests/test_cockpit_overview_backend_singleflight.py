import threading
import time
import unittest

from dalton_core.cockpit_plane import CockpitPlane


class OverviewSingleflightTests(unittest.TestCase):
    def plane(self):
        plane = CockpitPlane.__new__(CockpitPlane)
        plane._overview_condition = threading.Condition()
        plane._overview_building = False
        plane._overview_generation = 0
        plane._overview_result = None
        return plane

    def test_concurrent_readers_share_the_active_build(self):
        plane = self.plane()
        entered = threading.Event()
        release = threading.Event()
        calls = []

        def build():
            calls.append(object())
            entered.set()
            self.assertTrue(release.wait(2))
            return {"snapshot": len(calls)}

        plane._build_overview = build
        results = []
        first = threading.Thread(target=lambda: results.append(plane.overview()))
        second = threading.Thread(target=lambda: results.append(plane.overview()))
        first.start()
        self.assertTrue(entered.wait(2))
        second.start()
        time.sleep(0.02)
        release.set()
        first.join(2); second.join(2)
        self.assertEqual(len(calls), 1)
        self.assertEqual(results, [{"snapshot": 1}, {"snapshot": 1}])

    def test_sequential_reader_builds_a_fresh_snapshot(self):
        plane = self.plane()
        calls = []
        def build():
            calls.append(1)
            return {"snapshot": len(calls)}
        plane._build_overview = build
        self.assertEqual(plane.overview(), {"snapshot": 1})
        self.assertEqual(plane.overview(), {"snapshot": 2})

    def test_failed_builder_does_not_poison_the_next_reader(self):
        plane = self.plane()
        calls = []
        def build():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("broken snapshot")
            return {"snapshot": 2}
        plane._build_overview = build
        with self.assertRaisesRegex(RuntimeError, "broken snapshot"):
            plane.overview()
        self.assertEqual(plane.overview(), {"snapshot": 2})


if __name__ == "__main__":
    unittest.main()

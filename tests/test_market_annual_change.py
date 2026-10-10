import json, sqlite3, unittest
from dalton_core.cockpit_plane import CockpitPlane


class AnnualChangeTests(unittest.TestCase):
    def market(self, points):
        connection=sqlite3.connect(':memory:');connection.row_factory=sqlite3.Row
        self.addCleanup(connection.close)
        connection.execute('CREATE TABLE market_price_series_versions(series_ref TEXT,version_number INTEGER,record_json TEXT)')
        bars=[{'date':day,'close':value,'adj_close':value,'captured_at':day+'T23:00:00Z'} for day,value in points]
        connection.execute('INSERT INTO market_price_series_versions VALUES(?,?,?)',('series:synthetic',1,json.dumps({'company_ref':'company:synthetic','bars':bars,'currency':'USD'})))
        reader=object.__new__(CockpitPlane)
        return reader._market(connection)['company:synthetic']

    def test_single_bar_and_short_history_are_missing_instead_of_zero(self):
        for points in ([('2026-01-01','100')],[('2026-09-10','100'),('2026-09-11','110')]):
            view=self.market(points)
            self.assertIsNone(view['change_percent'])
            self.assertIsNone(view['change_since'])
            self.assertEqual(view['change_status'],'unavailable')

    def test_near_annual_endpoint_is_explicit_and_old_endpoint_gap_is_refused(self):
        view=self.market([('2025-09-10','100'),('2026-09-11','110')])
        self.assertEqual(view['change_percent'],10.0)
        self.assertEqual(view['change_since'],'2025-09-10')
        self.assertIsNone(self.market([('2025-08-01','100'),('2026-09-11','110')])['change_percent'])

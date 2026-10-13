import copy
import unittest
from dalton_core.operating_driver_source_plan import source_unit_target
from dalton_core.operating_driver_units import CONTRACT_REF
from dalton_core.directed_numeric_research import normalize_targets, DirectedNumericError


class SourceUnitPreflightTests(unittest.TestCase):
    def scope(self, label='Units sold'):
        return {'metric': {'ref':'units-sold','label':label,'unit':'units'},
                'period_start':'2026-01-01','period_end':'2026-03-31'}

    def test_exact_quantity_and_price_units_are_explicit_new_targets(self):
        for label, quote, unit, currency in (
            ('Units sold','Units sold (tons) | 40','tons',None),
            ('Units sold','Units sold (metric tons) | 40','metric_tons',None),
            ('Average selling price','Average selling price (USD per ton) | 12','currency_per_ton','USD'),
            ('Plant utilization','Plant utilization (percent) | 82','percent',None),
            ('Gas cost','Gas cost (USD per MMBtu) | 3','currency_per_mmbtu','USD')):
            with self.subTest(unit=unit):
                got=source_unit_target(self.scope(label),[{'excerpt':'2026-01-01/2026-03-31 '+quote}])
                self.assertEqual((got['unit'],got['currency']),(unit,currency))
                self.assertEqual(got['period'],'2026-01-01/2026-03-31')
                self.assertEqual(normalize_targets([got],contract_ref=CONTRACT_REF),[got])
                if unit not in ('percent','currency'):
                    with self.assertRaises(DirectedNumericError): normalize_targets([got])

    def test_material_availability_or_source_ambiguity_is_not_numeric_permission(self):
        for quote in ('Units sold were 40.', 'Average selling price ($ per ton) | 12',
                      'Units sold (metric tons) | 40\nUnits sold (short tons) | 40',
                      'Other companies Units (tons) | 40', 'Units sold (USD per kWh) | 40'):
            with self.subTest(quote=quote),self.assertRaises(ValueError):
                source_unit_target(self.scope(),[{'excerpt':'2026-01-01/2026-03-31 '+quote}])


if __name__=='__main__': unittest.main()

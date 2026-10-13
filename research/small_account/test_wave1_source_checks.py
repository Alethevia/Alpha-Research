import unittest
from research.small_account.wave1_source_checks import select_filings, acceptance, tender_ceiling


class SourceChecksTests(unittest.TestCase):
    def test_index_selection_numeric_excludes_amendments(self):
        text = '100|C|8-K|20250102|c\n9|B|8-K|20250102|b\n1|A|8-K/A|20250102|a'
        self.assertEqual([x[0] for x in select_filings(text)], ['9','100'])

    def test_no_acceptance_substitution_from_event_date(self):
        self.assertEqual(acceptance('<ACCEPTANCE-DATETIME>20250102083213'), '20250102083213')
        with self.assertRaises(ValueError):
            acceptance('CONFORMED PERIOD OF REPORT: 20250102')

    def test_tender_cost_and_capital_ceiling(self):
        self.assertEqual(tender_ceiling(2000,20,80,25), 78.75)
        self.assertEqual(tender_ceiling(1000,20,80,25), 48.75)
        self.assertEqual(tender_ceiling(2000,20,80,25,10,5), 78)

    def test_odd_lot_size_is_not_assumed_for_round_lot(self):
        for shares in (0,100,1.5):
            with self.assertRaises(ValueError):
                tender_ceiling(2000,shares,80,25)


if __name__ == '__main__':
    unittest.main()

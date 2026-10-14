"""Synthetic numerical checks; these fixtures are not market observations."""
from decimal import Decimal as D
import json
import unittest
from triangle_book_gate import analyze, clock_gates, convert


def book(price=10):
    return {'a': [{'p': price, 's': 1}, {'p': price*2, 's': 1}],
            'b': [{'p': price, 's': 1}, {'p': price/2, 's': 1}],
            't': '2026-10-14T19:00:00Z'}


class TriangleGateTests(unittest.TestCase):
    def test_buy_depth_and_received_base_fee(self):
        result, used = convert(20, book(), 'buy', '.01')
        self.assertEqual((result, used), (D('1.485'), 2))

    def test_sell_depth_and_received_quote_fee(self):
        self.assertEqual(convert('1.5', book(), 'sell', '.01'), (D('12.375'), 2))

    def test_depth_is_not_invented(self):
        for action, amount in [('buy', 31), ('sell', 3)]:
            with self.assertRaisesRegex(ValueError, 'insufficient'):
                convert(amount, book(), action, 0)

    def test_crossed_book_rejected(self):
        b = book(); b['b'][0]['p'] = 11
        with self.assertRaisesRegex(ValueError, 'crossed'):
            convert(1, b, 'buy', 0)

    def test_fee_free_identity_and_three_fees(self):
        books = {pair: book(p) for pair, p in [('BTC/USD', 100), ('ETH/BTC', .1), ('ETH/USD', 10)]}
        record = {'http_status': 200, 'received_utc': '2026-10-14T19:00:00+00:00',
                  'body': json.dumps({'orderbooks': books})}
        for fee in ['0', '.0025']:
            r = analyze(record, ['1'], fee)
            for route in r['routes'].values():
                self.assertEqual(route['size_cases'][0]['output_usd'], (1-D(fee))**3)

    def test_stale_skew_and_future_timestamps(self):
        a, b = book(), book(); b['t'] = '2026-10-14T19:00:01Z'
        gates = clock_gates({'a': a, 'b': b}, '2026-10-14T19:00:03+00:00', 2, .25)
        self.assertEqual(len(gates['clock_failures']), 2)
        gates = clock_gates({'b': b}, '2026-10-14T19:00:00+00:00', 2, .25)
        self.assertIn('BOOK_TIMESTAMP_AFTER_LOCAL_RECEIPT', gates['clock_failures'])

    def test_missing_pair_and_failed_request_not_zero_return(self):
        r = analyze({'http_status': 200, 'body': '{"orderbooks": {}}'}, ['1'], '0')
        self.assertFalse(r['data_valid'])
        self.assertFalse(analyze({'http_status': 429}, ['1'], '0')['data_valid'])

    def test_more_expensive_depth_cannot_improve_bound(self):
        b = book()
        small, _ = convert(5, b, 'buy', 0)
        large, _ = convert(20, b, 'buy', 0)
        self.assertLessEqual(large / 20, small / 5)


if __name__ == '__main__':
    unittest.main()

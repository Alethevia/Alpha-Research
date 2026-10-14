"""Read-only Alpaca book access and optimistic snapshot conversion bounds.

Not an order router or performance backtest. Fees are reserved in the received
asset. Continuous quantities ignore minimum sizes/rounding; sequential quotes
are not simultaneous fills. All requests and failures are retained privately.
"""
import argparse
from datetime import datetime, timezone
from decimal import Decimal, getcontext
import hashlib
import json
from pathlib import Path
import time
import urllib.error
import urllib.request

getcontext().prec = 40
D = Decimal
URL = ('https://data.alpaca.markets/v1beta3/crypto/us/latest/orderbooks'
       '?symbols=BTC%2FUSD%2CETH%2FBTC%2CETH%2FUSD')
ROUTES = {
    'USD_BTC_ETH_USD': [('BTC/USD', 'buy'), ('ETH/BTC', 'buy'), ('ETH/USD', 'sell')],
    'USD_ETH_BTC_USD': [('ETH/USD', 'buy'), ('ETH/BTC', 'sell'), ('BTC/USD', 'sell')],
}


def utc():
    return datetime.now(timezone.utc).isoformat()


def write_new(path, value):
    with Path(path).open('x') as f:
        json.dump(value, f, indent=2, default=str)
        f.write('\n')


def collect_one(spec_path, output, index):
    raw_spec = Path(spec_path).read_bytes()
    spec = json.loads(raw_spec)
    if not 0 <= index < spec['additional_requests']:
        raise ValueError('index outside fixed diagnostic budget')
    target = Path(output) / f'alpaca_fixed_{index:02d}.json'
    # Claim index before I/O: interruption remains an explicit failed attempt.
    write_new(target, {'status': 'STARTED_NOT_COMPLETED', 'index': index,
                       'sent_utc': utc()})
    record = {'source': 'Alpaca public market-data REST; no account authentication',
              'request_url': URL, 'sent_utc': utc(), 'index': index,
              'design_sha256': hashlib.sha256(raw_spec).hexdigest()}
    started = time.monotonic()
    try:
        req = urllib.request.Request(URL, headers={'User-Agent': 'small-account-research/1.0'})
        with urllib.request.urlopen(req, timeout=spec['timeout_seconds']) as response:
            record.update(http_status=response.status,
                          server_date=response.headers.get('Date'),
                          body=response.read().decode('utf-8'))
    except urllib.error.HTTPError as exc:
        record.update(http_status=exc.code, error=str(exc),
                      body=exc.read().decode('utf-8', errors='replace'))
    except (OSError, ValueError) as exc:
        record.update(http_status=None, error=f'{type(exc).__name__}: {exc}')
    record.update(received_utc=utc(), elapsed_seconds=time.monotonic()-started)
    # Replace only this attempt's placeholder, never another completed request.
    temporary = target.with_suffix('.tmp')
    write_new(temporary, record)
    temporary.replace(target)
    return {k: v for k, v in record.items() if k != 'body'}


def number(value):
    n = D(str(value))
    if not n.is_finite() or n <= 0:
        raise ValueError('price, size and input amount must be finite and positive')
    return n


def levels(book, side):
    result = [(number(x['p']), number(x['s'])) for x in book[side]]
    if not result or len({p for p, _ in result}) != len(result):
        raise ValueError('empty or duplicate-price depth')
    return sorted(result, reverse=side == 'b')


def validate_book(book):
    if levels(book, 'b')[0][0] > levels(book, 'a')[0][0]:
        raise ValueError('crossed book')
    datetime.fromisoformat(book['t'].replace('Z', '+00:00'))


def convert(amount, book, action, fee):
    amount = number(amount)
    fee = D(str(fee))
    if action not in ('buy', 'sell') or not 0 <= fee < 1:
        raise ValueError('invalid side or fee')
    validate_book(book)
    remaining, gross, used = amount, D(0), 0
    for price, size in levels(book, 'a' if action == 'buy' else 'b'):
        capacity = price * size if action == 'buy' else size
        spent = min(remaining, capacity)
        gross += spent / price if action == 'buy' else spent * price
        remaining -= spent
        used += 1
        if remaining == 0:
            return gross * (1 - fee), used
    raise ValueError('insufficient displayed depth; no extrapolated fill')


def clock_gates(books, received, max_age, max_skew):
    receipt = datetime.fromisoformat(received)
    times = [datetime.fromisoformat(b['t'].replace('Z', '+00:00')) for b in books.values()]
    # datetime truncates sub-microsecond digits; thresholds are seconds, not ns.
    ages = [(receipt - t).total_seconds() for t in times]
    skew = (max(times) - min(times)).total_seconds()
    errors = []
    if min(ages) < 0:
        errors.append('BOOK_TIMESTAMP_AFTER_LOCAL_RECEIPT')
    if max(ages) > max_age:
        errors.append('BOOK_AGE_EXCEEDS_DIAGNOSTIC_LIMIT')
    if skew > max_skew:
        errors.append('PAIR_TIMESTAMP_SKEW_EXCEEDS_DIAGNOSTIC_LIMIT')
    return {'max_book_age_seconds': max(ages), 'pair_skew_seconds': skew,
            'clock_gate_pass': not errors, 'clock_failures': errors}


def analyze(record, sizes, fee, max_age=2.0, max_skew=0.25):
    if record.get('http_status') != 200:
        return {'data_valid': False, 'error': record.get('error', 'request incomplete')}
    try:
        all_books = json.loads(record['body'], parse_float=D)['orderbooks']
        books = {pair: all_books[pair] for pair in ('BTC/USD', 'ETH/BTC', 'ETH/USD')}
        for b in books.values():
            validate_book(b)
        result = {'data_valid': True, **clock_gates(books, record['received_utc'], max_age, max_skew),
                  'execution_verified': False, 'routes': {}}
        for name, route in ROUTES.items():
            ratio = D(1)
            for pair, side in route:
                price = levels(books[pair], 'a' if side == 'buy' else 'b')[0][0]
                ratio *= 1 / price if side == 'buy' else price
            cases = []
            for raw_amount in sizes:
                amount, used = number(raw_amount), []
                try:
                    for pair, side in route:
                        amount, count = convert(amount, books[pair], side, fee)
                        used.append(count)
                    cases.append({'input_usd': raw_amount, 'output_usd': amount,
                                  'net_change_usd': amount - D(str(raw_amount)),
                                  'net_return': amount / D(str(raw_amount)) - 1,
                                  'levels_consumed_each_leg': used, 'depth_sufficient': True})
                except ValueError as exc:
                    cases.append({'input_usd': raw_amount, 'depth_sufficient': False, 'error': str(exc)})
            result['routes'][name] = {'gross_top_ratio': ratio,
                'net_top_ratio': ratio * (1-D(str(fee)))**3,
                'no_positive_snapshot_cycle_before_fees': ratio <= 1,
                'size_cases': cases}
        return result
    except (KeyError, ValueError, TypeError, ArithmeticError) as exc:
        return {'data_valid': False, 'error': f'{type(exc).__name__}: {exc}'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--design', required=True)
    parser.add_argument('--evidence', required=True)
    parser.add_argument('--collect-index', type=int)
    parser.add_argument('--result')
    args = parser.parse_args()
    if args.collect_index is not None:
        print(json.dumps(collect_one(args.design, args.evidence, args.collect_index)))
        return
    spec = json.loads(Path(args.design).read_text())
    names = ['alpaca_public_probe.json'] + [f'alpaca_fixed_{i:02d}.json' for i in range(spec['additional_requests'])]
    rows = []
    for name in names:
        path = Path(args.evidence) / name
        raw = path.read_bytes()
        record = json.loads(raw)
        rows.append({'raw_file': name, 'sha256': hashlib.sha256(raw).hexdigest(),
                     'sample_role': 'access_probe_seen_before_design' if name == names[0] else 'fixed_budget_access_diagnostic',
                     'sent_utc': record.get('sent_utc'), 'received_utc': record.get('received_utc'),
                     'http_status': record.get('http_status'), 'elapsed_seconds': record.get('elapsed_seconds'),
                     'analysis': analyze(record, spec['sizes_usd'], spec['fee_per_leg'],
                                         spec['max_book_age_seconds'], spec['max_pair_skew_seconds'])})
    result = {'evidence_type': 'ACTUAL_PUBLIC_SNAPSHOTS_OPTIMISTIC_BOUND_NOT_BACKTEST',
              'design_sha256': hashlib.sha256(Path(args.design).read_bytes()).hexdigest(),
              'observations': rows, 'performance_backtests': 0, 'trades_placed': 0}
    write_new(args.result, result)
    print(json.dumps({'requests': len(rows), 'valid': sum(x['analysis']['data_valid'] for x in rows)}))


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""Independent saved-price, timing, cash and registration audit for I037."""
import argparse
from datetime import datetime, time, timedelta
import json
from pathlib import Path

try:
    from .m2_backtest import read_bars, sha256, save_json
    from .experiment_registry import load_events
except ImportError:
    from m2_backtest import read_bars, sha256, save_json
    from experiment_registry import load_events


def audit(data_dir, output):
    summary = json.loads((output / 'summary.json').read_text())
    manifest = json.loads((output / 'data_manifest.json').read_text())
    assert sha256(output / 'data_manifest.json') == summary['data_manifest_sha256']
    for f in manifest['files']:
        assert sha256(data_dir / f['file']) == f['sha256']
    assert sha256(data_dir / 'requests.json') == manifest['requests_sha256']
    assert sha256(output / 'signals.json') == summary['signals_sha256']
    signals = json.loads((output / 'signals.json').read_text())
    signal_by_date = {r['date']: r for r in signals}
    previous = {'QQQ': False, 'SPY': False}
    eligible = 0
    for r in signals:
        if r['eligible']:
            assert r['fit_start'] <= r['fit_end'] < r['date']
            assert r['fit_sessions'] == 60 and r['model']['beta'] > 0
            assert r['candidate'] == {'QQQ': r['z'] <= -1.5, 'SPY': r['z'] >= 1.5}
            assert r['lagged'] == previous
            previous = r['candidate']
            eligible += 1
    assert eligible == summary['eligible_sessions']
    events = load_events(output / 'experiments.jsonl')
    n = summary['account_scenarios'] + summary['diagnostics']
    assert n == 38 and len(events) == 2*n
    assert all(e['event'] == 'registered' for e in events[:n])
    assert all(e['event'] == 'completed' for e in events[n:])
    specs = {e['experiment_id']: e['spec'] for e in events[:n]}
    assert set(specs) == {e['experiment_id'] for e in events[n:]}
    datasets = {s: {b.timestamp: b for b in read_bars(data_dir, s)[0]} for s in ('SPY', 'QQQ')}
    checked, max_error, errors = 0, 0.0, []
    for event in events[n:]:
        path = output / event['artifact_name']
        assert sha256(path) == event['artifact_sha256']
        r = json.loads(path.read_text())
        spec = specs[event['experiment_id']]
        if 'error' in r:
            errors.append(event['experiment_id'])
            continue
        assert r['code_revision'] == spec['code_revision'] == summary['code_revision']
        if 'trades' not in r:
            continue
        bars = datasets[r['symbol']]
        fee, slip = r['cost_case']['cash_fee_per_order_usd'], r['cost_case']['slippage_bps_per_side']/10000
        pnl_sum = friction = 0.0
        seen = set()
        for t in r['trades']:
            enter, leave = datetime.fromisoformat(t['entry_time']), datetime.fromisoformat(t['exit_time'])
            d = enter.date().isoformat()
            assert d not in seen and enter.time() == time(10, 5) and enter < leave and enter.date() == leave.date()
            seen.add(d)
            row = signal_by_date[d]
            assert row['eligible']
            if r['rule'] != 'clock_only':
                field = 'candidate' if r['rule'] == 'relative_laggard' else 'lagged'
                assert row[field][r['symbol']]
            q = t['quantity']
            assert isinstance(q, int) and q > 0
            buy, sell = bars[enter].open * (1+slip), bars[leave].open * (1-slip)
            pnl = q * (sell-buy) - 2*fee
            pnl_sum += pnl
            friction += q*(bars[enter].open+bars[leave].open)*slip
            low = min(bars[enter.replace(hour=9, minute=m)].low for m in (30,35,40,45,50,55))
            assert t['stop'] == low
            planned = q*(buy-low*(1-slip))+2*fee
            assert planned <= t['risk_budget_usd'] + 1e-8
            max_error = max(max_error, abs(pnl-t['net_pnl']), abs(planned-t['planned_risk_usd']), abs(buy-t['entry_fill']), abs(sell-t['exit_fill']))
            if t['exit_reason'] == 'close_below_opening_low':
                triggers = []
                cursor = enter
                while cursor < leave:
                    if cursor in bars and bars[cursor].close <= low:
                        triggers.append(cursor)
                    cursor += timedelta(minutes=5)
                assert triggers and leave >= min(triggers) + timedelta(minutes=10)
            checked += 1
        m = r['metrics']
        months = 12 if r['period']['full_calendar_year'] else 9
        assert m['overhead_usd'] == months*r['cost_case']['monthly_overhead_usd']
        assert m['round_trips'] == len(r['trades']) <= m['active_model_sessions']
        max_error = max(max_error, abs(m['final_equity']-(2000+pnl_sum-m['overhead_usd'])),
                        abs(m['assumed_slippage_usd']-friction), abs(m['cash_fees_usd']-2*fee*len(r['trades'])))
        assert all(v.get('settled_cash',0) >= -1e-8 for v in r['equity_observations'])
        assert m['annualized_return'] is None
    assert max_error < 1e-7
    result = {'status': 'passed' if not errors else 'errors_preserved', 'journal_events_verified': len(events),
              'all_specs_before_any_case_outcome': True, 'artifact_hashes_verified': n,
              'raw_csv_hashes_verified': len(manifest['files']), 'requests_hash_verified': True,
              'eligible_past_only_signal_dates_checked': eligible, 'scenario_trade_records_checked': checked,
              'maximum_cash_price_reconciliation_error_usd': max_error, 'error_cases': errors,
              'scope': 'Integrity/accounting/timing only; scenario trades repeat observations and are not independent samples. No actual quote fills or profit validation.'}
    recovery = output / 'journal_recovery.json'
    if recovery.exists():
        result['late_completion_recovery'] = json.loads(recovery.read_text())
    save_json(output / 'audit.json', result)
    return result


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--data-dir', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    print(json.dumps(audit(a.data_dir, a.output), indent=2))

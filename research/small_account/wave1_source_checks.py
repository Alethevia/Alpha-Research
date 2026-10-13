#!/usr/bin/env python3
"""Offline source inventory and explicitly hypothetical tender arithmetic."""
import argparse
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re


def select_filings(text, count=3):
    rows = []
    for line in text.splitlines():
        fields = line.split('|')
        if len(fields) == 5 and fields[2] == '8-K':
            if fields[3].replace('-', '') != '20250102':
                raise ValueError('Unexpected filing date')
            rows.append(fields)
    return sorted(rows, key=lambda f: (int(f[0]), f[4]))[:count]


def acceptance(text):
    found = re.search(r'<ACCEPTANCE-DATETIME>(\d{14})', text)
    if not found:
        raise ValueError('No SEC acceptance timestamp; do not substitute event date')
    return found.group(1)


def tender_ceiling(capital, shares, conditional_payout, fees, holding_cost=0, taxes=0):
    if not isinstance(shares, int) or not 0 < shares < 100:
        raise ValueError('Illustration requires integer odd-lot quantity 1..99')
    c, pay, f, h, t = map(lambda x: Decimal(str(x)), (capital, conditional_payout, fees, holding_cost, taxes))
    if min(c, pay) <= 0 or min(f, h, t) < 0:
        raise ValueError('Invalid cash assumptions')
    return float(min((c-f-h-t)/shares, pay-(f+h+t)/shares))


def run(folder):
    master = (folder/'master.20250102.idx').read_bytes()
    manifest = json.loads((folder/'i010_intake_manifest.json').read_text())
    assert hashlib.sha256(master).hexdigest() == manifest['index_sha256']
    selected = select_filings(master.decode())
    records = []
    for fields, source in zip(selected, manifest['records'], strict=True):
        assert '|'.join(fields) == source['index_row']
        raw = (folder/source['file']).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == source['sha256']
        records.append({'cik': fields[0], 'company': fields[1], 'acceptance_et_compact': acceptance(raw.decode()),
                        'url': source['url'], 'sha256': source['sha256'], 'first_public_timestamp_verified': False})
    costs = [{'assumed_all_in_cost_usd': fees,
              'conditional_80_dollar_payout_max_entry_per_share': tender_ceiling(2000,20,80,fees),
              'retrospective_92_dollar_payout_max_entry_per_share': tender_ceiling(2000,20,92,fees)} for fees in (0,25,50)]
    illustrative_pnl = [{'disposition_price': p, 'net_pnl_usd': float(Decimal(20)*(Decimal(p)-Decimal(79))-Decimal(25)),
                        'total_account_return': float((Decimal(20)*(Decimal(p)-Decimal(79))-Decimal(25))/Decimal(2000))} for p in (80,92,60)]
    return {'source_intake': {'date': '2025-01-02', 'population_8k_count': manifest['population_8k_count'],
                              'selected_count': len(records), 'selection': 'first three by numeric CIK then accession path',
                              'coverage_limit': 'One date, low-CIK convenience ordering; not representative or a return study.',
                              'records': records},
            'tender_synthetic_arithmetic': {'evidence_type': 'synthetic', 'capital': 2000, 'shares': 20,
                'fee_ceiling_cases': costs, 'assumed_entry_79_fee_25_cases': illustrative_pnl,
                'taxes_and_holding_cost_assumed_for_calculator_only': 0,
                'prices_are_hypothetical_not_market_observations': True,
                'limits': '80 is conditional on completion under unchanged original terms, not a capital guarantee. 92 was learned after expiry. Account fees, tax status, aggregate holdings and broker deadline remain unverified. No recommendation or historical PnL.'}}


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source-dir', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    if a.output.exists():
        raise ValueError('Do not overwrite prior source output')
    a.output.write_text(json.dumps(run(a.source_dir), indent=2, ensure_ascii=False)+'\n')

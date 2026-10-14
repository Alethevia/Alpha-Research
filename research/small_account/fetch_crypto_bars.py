#!/usr/bin/env python3
"""Download public Alpaca US crypto bars for research, without account access.

Only sends GET requests to the market-data endpoint. The feed can include
quote-derived bars with zero reported trade volume. Bars are not order fills.
"""

import argparse
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path
import time
import urllib.error
import urllib.parse
import urllib.request


ENDPOINT = "https://data.alpaca.markets/v1beta3/crypto/us/bars"


def fetch(symbols: list[str], start: str, end: str, output: Path) -> dict:
    raw = {symbol: [] for symbol in symbols}
    page_token = None
    pages = []
    seen_tokens = set()
    while True:
        params = {"symbols": ",".join(symbols), "timeframe": "1Hour", "start": start,
                  "end": end, "limit": 10000, "sort": "asc"}
        if page_token:
            params["page_token"] = page_token
        url = ENDPOINT + "?" + urllib.parse.urlencode(params)
        for attempt in range(4):
            try:
                request = urllib.request.Request(url, headers={"User-Agent": "AlphaResearch/0.1"})
                with urllib.request.urlopen(request, timeout=45) as response:
                    body = response.read()
                break
            except urllib.error.HTTPError as error:
                if error.code not in (429, 500, 502, 503, 504) or attempt == 3:
                    raise
                time.sleep(min(2**attempt, 8))
        decoded = json.loads(body)
        for symbol in symbols:
            raw[symbol].extend(decoded.get("bars", {}).get(symbol, []))
        pages.append({"request": params, "response_sha256": hashlib.sha256(body).hexdigest()})
        print(json.dumps({"page": len(pages), "bar_counts": {s: len(b) for s, b in raw.items()}}), flush=True)
        page_token = decoded.get("next_page_token")
        if not page_token:
            break
        if page_token in seen_tokens:
            raise ValueError("repeated pagination token")
        seen_tokens.add(page_token)
    result = {
        "endpoint": ENDPOINT, "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
        "start_inclusive": start, "end_requested": end,
        "note": "Provider may include a bar at end; runner enforces exclusive sample end.",
        "pages": pages, "bars": raw,
    }
    data = json.dumps(result, separators=(",", ":"), allow_nan=False).encode()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(gzip.compress(data, mtime=0))
    manifest = {k: v for k, v in result.items() if k != "bars"}
    manifest["snapshot_sha256"] = hashlib.sha256(output.read_bytes()).hexdigest()
    manifest["bar_counts"] = {s: len(b) for s, b in raw.items()}
    manifest["evidence_type"] = "historical_price_bars_not_execution_evidence"
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="2023-12-01T00:00:00Z")
    parser.add_argument("--end", default="2026-01-01T00:00:00Z")
    parser.add_argument("--symbols", nargs="+", default=["BTC/USD", "ETH/USD"])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = fetch(args.symbols, args.start, args.end, args.output)
    args.output.with_suffix(".manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest, indent=2))

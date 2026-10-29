# Alpha Research

A collection of experimental quantitative research code: factor models, point-in-time data utilities, portfolio construction, backtesting and validation tests.

**Research status:** no production-readiness certification, independently audited return, or novel causal discovery is claimed. Earlier README performance tables and “institutional grade” conclusions have been withdrawn because this repository does not establish a complete, independently reproduced chain from historical data snapshots to those numbers. Existing research-review documents remain historical notes, not certification.

## Install and run offline checks

Python 3.11+:

```bash
python -m pip install -e ".[dev]"
python -m pytest tests -q -o addopts=
alpha-research --help
```

Optional external providers, broker integration and LLM dependencies are listed in `requirements-all.txt`. Keep credentials outside version control. The many `scripts/run_*.py` files are distinct research variants, not interchangeable production entry points. They have not all been reproduced against real data in this audit; historical experiments share periods and must not be described as independent untouched tests.

## What the tests establish

Tests exercise accounting, time ordering, point-in-time filtering, risk rules and other components on fixtures. Passing them does not validate every historical script, investment outcome, or LLM decision. Current-universe backtests can suffer survivorship bias; anonymizing tickers does not by itself prove the absence of LLM training-data leakage. See [AUDIT.md](AUDIT.md).

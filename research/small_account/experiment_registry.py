#!/usr/bin/env python3
"""Single-writer research journal: register a frozen spec, then record an outcome.

Hash chaining detects accidental edits, not a malicious rewrite of the journal.
Registration proves what this local journal records, not that a human has never
seen the test data. It does not approve a strategy or authorize live trading.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path


def digest(value: dict) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def load_events(path: Path) -> list[dict]:
    if not path.exists():
        return []
    events = []
    previous = "0" * 64
    for index, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        event = json.loads(line)
        body = {k: v for k, v in event.items() if k != "event_sha256"}
        if event.get("previous_sha256") != previous or event.get("event_sha256") != digest(body):
            raise ValueError(f"journal hash mismatch at event {index}")
        previous = event["event_sha256"]
        events.append(event)
    return events


def append_event(path: Path, events: list[dict], payload: dict) -> dict:
    event = {
        **payload,
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "previous_sha256": events[-1]["event_sha256"] if events else "0" * 64,
    }
    event["event_sha256"] = digest(event)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as output:
        output.write(json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n")
        output.flush()
        os.fsync(output.fileno())
    return event


def register(path: Path, experiment_id: str, spec: dict) -> dict:
    events = load_events(path)
    if not experiment_id.strip():
        raise ValueError("experiment_id is required")
    if any(event.get("experiment_id") == experiment_id for event in events):
        raise ValueError("experiment_id already exists; revisions need a new experiment")
    required = (
        "hypothesis", "data_provenance", "data_sha256", "code_revision", "parameters",
        "sample_split", "cost_model", "primary_metric", "rejection_rules", "evidence_type",
    )
    missing = [key for key in required if key not in spec or spec[key] is None]
    if missing:
        raise ValueError(f"missing frozen spec fields: {', '.join(missing)}")
    data_hash = spec["data_sha256"]
    if not isinstance(data_hash, str) or len(data_hash) != 64 or any(
        char not in "0123456789abcdef" for char in data_hash
    ):
        raise ValueError("data_sha256 must be a lowercase SHA-256 hex digest")
    if spec["evidence_type"] not in ("synthetic", "historical_development", "forward_paper"):
        raise ValueError("use an explicit supported evidence_type")
    return append_event(path, events, {
        "event": "registered", "experiment_id": experiment_id,
        "spec_sha256": digest(spec), "spec": spec,
    })


def complete(path: Path, experiment_id: str, outcome: str, artifact: Path) -> dict:
    events = load_events(path)
    registrations = [e for e in events if e.get("experiment_id") == experiment_id and e["event"] == "registered"]
    if len(registrations) != 1:
        raise ValueError("register the experiment before recording its outcome")
    if any(e.get("experiment_id") == experiment_id and e["event"] == "completed" for e in events):
        raise ValueError("outcome already recorded; corrections need a new experiment")
    if outcome not in ("completed", "rejected", "error"):
        raise ValueError("outcome must be completed, rejected, or error")
    result = json.loads(artifact.read_text(encoding="utf-8"))
    if result.get("evidence_type") != registrations[0]["spec"]["evidence_type"]:
        raise ValueError("result evidence_type differs from the registered spec")
    return append_event(path, events, {
        "event": "completed", "experiment_id": experiment_id,
        "spec_sha256": registrations[0]["spec_sha256"], "outcome": outcome,
        "artifact_name": artifact.name,
        "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
    })


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal", type=Path, required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    start = sub.add_parser("register")
    start.add_argument("experiment_id")
    start.add_argument("--spec", type=Path, required=True)
    end = sub.add_parser("complete")
    end.add_argument("experiment_id")
    end.add_argument("--outcome", choices=("completed", "rejected", "error"), required=True)
    end.add_argument("--artifact", type=Path, required=True)
    sub.add_parser("inspect")
    args = parser.parse_args()
    if args.command == "register":
        result = register(args.journal, args.experiment_id, json.loads(args.spec.read_text(encoding="utf-8")))
    elif args.command == "complete":
        result = complete(args.journal, args.experiment_id, args.outcome, args.artifact)
    else:
        events = load_events(args.journal)
        registered = {e["experiment_id"] for e in events if e["event"] == "registered"}
        finished = {e["experiment_id"] for e in events if e["event"] == "completed"}
        result = {
            "registered_experiments_in_this_journal": len(registered),
            "finished_experiments": len(finished),
            "unfinished_experiments": sorted(registered - finished),
            "includes_failed_and_incomplete_attempts": True,
            "other_journals_and_legacy_trials_must_also_be_counted": True,
        }
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Verify one PropLine receipt; if storage alone blocked it, attempt bounded cleanup."""
import argparse
import sys
from datetime import datetime, timedelta, timezone

if __package__:
    from .maintain_storage import maintain
    from .verify_freshness import (
        parse_timestamp, propline_receipt_failures, propline_storage_admission_failures, read_status,
    )
else:
    from maintain_storage import maintain
    from verify_freshness import (
        parse_timestamp, propline_receipt_failures, propline_storage_admission_failures, read_status,
    )

RECOVERY_RESERVE = 150  # workflow timeout 120s plus 30s for scan admission
CRITICAL_ONLY = ["storage critical"]


def receipt_signature(payload):
    row, = [r["lastRun"] for r in payload["sources"] if r.get("id") == "propline"]
    return tuple(row[k] for k in ("status", "recordsWritten", "startedAt", "completedAt"))


def verify_or_recover(load, cleanup, *, start, end, cycle, clock):
    def evaluate(payload):
        return propline_receipt_failures(
            payload, expected_start=start, expected_end=end, cycle_key=cycle, now=clock())

    def measured_headroom(payload, completed):
        # A cached pre-quote good reading cannot clear a post-quote failure.
        measured = parse_timestamp(((payload.get("storage") or {}).get("capacity") or {}).get("maintenanceMeasuredAt"))
        if measured is None or measured < completed:
            return ["no post-quote measured capacity after storage recovery"]
        return propline_storage_admission_failures(payload, clock())

    payload = load()
    failures = evaluate(payload)
    if failures and failures != CRITICAL_ONLY:
        return failures
    # Unlike the existing no-recovery path, recovery is only for a known
    # canonical slot that can still fit cleanup and leave scan time.
    slot = parse_timestamp(cycle)
    completed = parse_timestamp(end)
    if slot is None or completed is None:
        return ["bounded storage recovery requires a canonical slot"]
    boundary = min(slot + timedelta(minutes=30), completed + timedelta(minutes=5))
    if not failures:
        if (boundary - clock()).total_seconds() < 30:
            return ["not enough time remains for recovered scan admission"]
        return measured_headroom(payload, completed)
    if (boundary - clock()).total_seconds() <= RECOVERY_RESERVE:
        return failures + ["insufficient time for bounded same-slot storage recovery"]
    original = receipt_signature(payload)
    cleanup()
    after = load()
    verified = evaluate(after)
    if verified:
        return verified
    if receipt_signature(after) != original:
        return ["PropLine receipt changed during storage recovery"]
    if (boundary - clock()).total_seconds() < 30:
        return ["not enough time remains for recovered scan admission"]
    return measured_headroom(after, completed)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("url")
    p.add_argument("--propline-started-at", required=True)
    p.add_argument("--propline-completed-at", required=True)
    p.add_argument("--cycle-key", required=True)
    a = p.parse_args()
    try:
        failures = verify_or_recover(
            lambda: read_status(a.url), lambda: maintain(target=84, max_passes=3),
            start=a.propline_started_at, end=a.propline_completed_at, cycle=a.cycle_key,
            clock=lambda: datetime.now(timezone.utc),
        )
    except Exception:
        # Never retry an unknown cleanup outcome or log upstream details here.
        failures = ["could not verify safe storage recovery"]
    if failures:
        print("Freshness gate failed: " + "; ".join(failures), file=sys.stderr)
        return 1
    print("Durable PropLine receipt and storage verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

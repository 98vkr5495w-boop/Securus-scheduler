#!/usr/bin/env python3
"""Fail closed when decision-critical Securus sources are stale."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import math
import sys
import time
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


MAX_AGE_MINUTES = {
    "mlb-stats-api": 45,
    "action-network": 45,
    "kalshi": 45,
    "propline": 5,
    "open-meteo": 90,
    "sleeper-nfl": 150,
    "nba-stats": 180,
    "nba-official-injuries": 150,
    "nflverse": 26 * 60,
    "baseball-savant": 26 * 60,
}


def nba_reporting_inactive(now: datetime) -> bool:
    eastern = now.astimezone(ZoneInfo("America/New_York"))
    # NBA's verified 2026-27 opener is October 20. Collect from October 19
    # for opening-night reports. Unknown future seasons keep strict checks.
    # https://pr.nba.com/2026-27-nba-regular-season-schedule/
    return eastern.month in (8, 9) or (
        eastern.year == 2026 and eastern.month == 10 and eastern.day < 19
    )


def parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.utcoffset() is not None else None


def freshness_failures(
    payload: dict[str, object],
    wanted: set[str],
    now: datetime,
) -> list[str]:
    latest = {
        str(row.get("id")): row.get("lastRun") or {}
        for row in (payload.get("sources") or [])
        if isinstance(row, dict)
    }
    failures: list[str] = []
    for source in sorted(wanted):
        run = latest.get(source)
        if not isinstance(run, dict):
            failures.append(f"{source} missing")
            continue
        completed_at = parse_timestamp(run.get("completedAt"))
        if run.get("status") != "SUCCEEDED" or completed_at is None:
            failures.append(f"{source} unhealthy")
            continue
        if source == "nba-official-injuries" and int(run.get("recordsWritten") or 0) < 1:
            failures.append("nba-official-injuries has no verified active-season report")
            continue
        age_minutes = (now - completed_at).total_seconds() / 60
        if age_minutes < 0:
            failures.append(f"{source} has a future completion timestamp")
        if age_minutes > MAX_AGE_MINUTES.get(source, 75):
            failures.append(f"{source} stale (last success {run['completedAt']}; age {age_minutes:.1f}m; limit {MAX_AGE_MINUTES.get(source, 75)}m)")

    capacity = ((payload.get("storage") or {}).get("capacity") or {})
    utilization = capacity.get("utilizationPercent")
    if capacity.get("capacityState") not in ("NORMAL", "WARNING", "CRITICAL") or type(utilization) not in (int, float) or not math.isfinite(utilization) or utilization < 0:
        failures.append("storage health unavailable")
    elif capacity.get("capacityState") == "CRITICAL" or utilization >= 85 or capacity.get("maintenanceState") == "CRITICAL":
        failures.append("storage critical")
    return failures


def propline_storage_admission_failures(payload, now):
    capacity = (payload.get("storage") or {}).get("capacity") or {}
    used, limit = capacity.get("actualBytes"), capacity.get("safetyCapacityBytes")
    measured = parse_timestamp(capacity.get("maintenanceMeasuredAt"))
    if not measured or not 0 <= (now - measured).total_seconds() <= 300:
        return ["No recent measured maintenance capacity for PropLine"]
    utilization = capacity.get("utilizationPercent")
    if (capacity.get("sizeSource") != "D1_META"
        or capacity.get("capacityState") not in ("NORMAL", "WARNING")
        or capacity.get("maintenanceState") not in ("NORMAL", "WARNING")
        or type(used) is not int or type(limit) is not int or not 0 < used <= limit
        or type(utilization) not in (float, int) or not math.isfinite(utilization)
        or abs(utilization - 100 * used / limit) > .02
        or utilization > 84 or 100 * used > 84 * limit):
        return ["Verified storage has insufficient admission headroom for PropLine"]
    return []


def propline_receipt_failures(payload, *, expected_start, expected_end, cycle_key, now):
    """Bind the latest durable result to this workflow's exact PropLine POST."""
    start, end = parse_timestamp(expected_start), parse_timestamp(expected_end)
    cycle = parse_timestamp(cycle_key)
    if not start or not end or (cycle_key and (
        not cycle or cycle.minute not in (7, 37) or cycle.second or cycle.microsecond
        or not cycle <= start < cycle + timedelta(minutes=30)
    )):
        return ["PropLine current-cycle collection receipt missing or out of slot"]
    if not start <= end <= now or (now - end).total_seconds() > 300:
        return ["PropLine current-cycle collection receipt outside five-minute window"]
    runs = [row.get("lastRun") for row in (payload.get("sources") or [])
            if isinstance(row, dict) and row.get("id") == "propline"]
    if len(runs) != 1 or not isinstance(runs[0], dict):
        return ["PropLine durable collection receipt missing or ambiguous"]
    run = runs[0]
    if (run.get("status") != "SUCCEEDED"
        or type(run.get("recordsWritten")) is not int or run["recordsWritten"] < 1
        or parse_timestamp(run.get("startedAt")) != start
        or parse_timestamp(run.get("completedAt")) != end):
        return ["PropLine durable receipt does not match this successful collection"]
    return freshness_failures(payload, {"propline"}, now)


def verify_with_rechecks(load, wanted, *, clock=lambda: datetime.now(timezone.utc), sleep=time.sleep):
    """Cross the public status cache TTL without refreshing sources or relaxing limits."""
    failures = []
    for attempt in range(4):
        try:
            failures = freshness_failures(load(), wanted, clock())
        except Exception:
            failures = ["Securus status is unavailable"]
        if not failures or any(failure.startswith("storage") for failure in failures):
            return failures
        if attempt < 3:
            print("Rechecking public status after cache expiry: " + ", ".join(failures), file=sys.stderr)
            sleep(15)
    return failures


def read_status(url):
    request = Request(
        f"{url.rstrip('/')}/api/data-sources",
        headers={"Accept": "application/json", "Cache-Control": "no-cache", "User-Agent": "Securus-Public-Watchdog/2.0"},
    )
    with urlopen(request, timeout=20) as response:
        if "application/json" not in response.headers.get("Content-Type", "").lower():
            raise ValueError("status is not JSON")
        raw = response.read(524289)
    if len(raw) > 524288:
        raise ValueError("status exceeds size limit")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("status is not an object")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("--source", action="append", dest="sources", default=[])
    gate_mode = parser.add_mutually_exclusive_group()
    gate_mode.add_argument("--current-propline-receipt", action="store_true")
    gate_mode.add_argument("--propline-storage-admission", action="store_true")
    parser.add_argument("--propline-started-at", default="")
    parser.add_argument("--propline-completed-at", default="")
    parser.add_argument("--cycle-key", default="")
    args = parser.parse_args()

    if args.propline_storage_admission:
        try:
            failures = propline_storage_admission_failures(read_status(args.url), datetime.now(timezone.utc))
        except Exception:
            failures = ["PropLine storage measurement unavailable"]
        if failures:
            print("Storage admission failed: " + ", ".join(failures) + ".", file=sys.stderr)
            return 1
        print("Recent measured capacity is within PropLine admission bounds.")
        return 0

    if args.current_propline_receipt:
        # Status snapshots can briefly lag a committed write; read-only
        # rechecks never retry the POST or extend the five-minute age gate.
        for attempt in range(4):
            try:
                failures = propline_receipt_failures(
                    read_status(args.url), expected_start=args.propline_started_at,
                    expected_end=args.propline_completed_at, cycle_key=args.cycle_key,
                    now=datetime.now(timezone.utc),
                )
            except Exception:
                failures = ["PropLine current-cycle status unavailable"]
            recheckable = all(failure in {
                "PropLine durable collection receipt missing or ambiguous",
                "PropLine durable receipt does not match this successful collection",
                "PropLine current-cycle status unavailable",
            } for failure in failures)
            if not failures or not recheckable or attempt == 3:
                break
            time.sleep(15)
        if failures:
            print("Freshness gate failed: " + ", ".join(failures) + ".", file=sys.stderr)
            return 1
        print("Committed PropLine receipt matched this collection within five minutes.")
        return 0

    now = datetime.now(timezone.utc)
    wanted = set(args.sources or (MAX_AGE_MINUTES.keys() - {"propline"}))
    if nba_reporting_inactive(now):
        wanted.difference_update({"nba-stats", "nba-official-injuries"})
    failures = verify_with_rechecks(lambda: read_status(args.url), wanted)

    if failures:
        print("Freshness gate failed: " + ", ".join(failures) + ".", file=sys.stderr)
        return 1
    print(f"Freshness gate passed for {len(wanted)} decision-critical sources.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

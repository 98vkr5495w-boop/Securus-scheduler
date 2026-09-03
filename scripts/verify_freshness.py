#!/usr/bin/env python3
"""Fail closed when decision-critical Securus sources are stale."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import sys
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


MAX_AGE_MINUTES = {
    "mlb-stats-api": 45,
    "action-network": 45,
    "kalshi": 45,
    "climate": 60,
    "open-meteo": 90,
    "sleeper-nfl": 150,
    "nba-stats": 180,
    "nba-official-injuries": 150,
    "nflverse": 26 * 60,
    "baseball-savant": 26 * 60,
}


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
        for row in payload.get("sources", [])
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
        if age_minutes > MAX_AGE_MINUTES.get(source, 75):
            failures.append(f"{source} stale")

    capacity = ((payload.get("storage") or {}).get("capacity") or {})
    if capacity.get("capacityState") == "CRITICAL":
        failures.append("storage critical")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("--source", action="append", dest="sources", default=[])
    args = parser.parse_args()

    request = Request(
        f"{args.url.rstrip('/')}/api/data-sources",
        headers={"Accept": "application/json", "User-Agent": "Securus-Public-Watchdog/1.0"},
    )
    try:
        with urlopen(request, timeout=30) as response:
            payload = json.load(response)
    except Exception:
        print("Freshness gate failed: Securus status is unavailable.", file=sys.stderr)
        return 1

    now = datetime.now(timezone.utc)
    wanted = set(args.sources or MAX_AGE_MINUTES)
    if now.astimezone(ZoneInfo("America/New_York")).month in (8, 9):
        wanted.difference_update({"nba-stats", "nba-official-injuries"})
    failures = freshness_failures(payload, wanted, now)

    if failures:
        print("Freshness gate failed: " + ", ".join(failures) + ".", file=sys.stderr)
        return 1
    print(f"Freshness gate passed for {len(wanted)} decision-critical sources.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Fail closed when no paper market has fresh decision-critical sources.

Securus itself forces a sport to NO_BET whenever one of that sport's required
sources is stale, failed, or missing. This gate mirrors the same per-market
source requirements so a supplemental feed outage (for example ESPN answering
403 while the NBA is in season) withholds only the affected market instead of
blocking the paper scan for every sport. The scan is still withheld entirely
when no market has fresh inputs or when storage is critical.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import sys
from urllib.request import Request, urlopen


# Keep this table identical to the Site's PAPER_SOURCE_FRESHNESS_REQUIREMENTS.
MARKET_REQUIREMENTS: dict[str, tuple[tuple[str, int], ...]] = {
    "MLB": (
        ("mlb-stats-api", 45),
        ("action-network", 45),
        ("kalshi", 45),
        ("open-meteo", 90),
        ("baseball-savant", 26 * 60),
    ),
    "NFL": (
        ("nflverse", 26 * 60),
        ("sleeper-nfl", 150),
        ("action-network", 45),
        ("kalshi", 45),
        ("open-meteo", 90),
    ),
    "NBA": (
        ("nba-stats", 180),
        ("espn", 180),
        ("action-network", 45),
        ("kalshi", 45),
    ),
    "CLIMATE": (("climate", 60),),
}


def parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.utcoffset() is not None else None


def is_nba_offseason(now: datetime) -> bool:
    # A successful zero-record NBA run is normal in the August/September offseason.
    return now.month in (8, 9)


def source_problem(run: object, max_age_minutes: int, now: datetime) -> str | None:
    """Return why a source cannot support a paper decision, or None when fresh."""
    if not isinstance(run, dict) or not run:
        return "missing"
    completed_at = parse_timestamp(run.get("completedAt"))
    if run.get("status") != "SUCCEEDED" or completed_at is None:
        return "unhealthy"
    age_minutes = (now - completed_at).total_seconds() / 60
    if age_minutes > max_age_minutes:
        return "stale"
    return None


def evaluate_markets(
    payload: dict, now: datetime, markets: list[str] | None = None
) -> dict[str, list[str]]:
    """Map each evaluated market to its blocking source problems (empty = fresh)."""
    latest = {
        str(row.get("id")): row.get("lastRun") or {}
        for row in payload.get("sources", [])
        if isinstance(row, dict)
    }
    wanted = [market for market in MARKET_REQUIREMENTS if not markets or market in markets]
    if is_nba_offseason(now):
        wanted = [market for market in wanted if market != "NBA"]
    results: dict[str, list[str]] = {}
    for market in wanted:
        problems: list[str] = []
        for source, max_age_minutes in MARKET_REQUIREMENTS[market]:
            problem = source_problem(latest.get(source), max_age_minutes, now)
            if problem:
                problems.append(f"{source} {problem}")
        results[market] = problems
    return results


def storage_is_critical(payload: dict) -> bool:
    capacity = ((payload.get("storage") or {}).get("capacity") or {})
    return capacity.get("capacityState") == "CRITICAL"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument(
        "--market",
        action="append",
        dest="markets",
        default=[],
        choices=sorted(MARKET_REQUIREMENTS),
        help="restrict the gate to these paper markets (default: every market)",
    )
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
    if not isinstance(payload, dict):
        print("Freshness gate failed: Securus status is unreadable.", file=sys.stderr)
        return 1

    now = datetime.now(timezone.utc)
    results = evaluate_markets(payload, now, args.markets)
    fresh = [market for market, problems in results.items() if not problems]
    withheld = [
        f"{market} ({', '.join(problems)})"
        for market, problems in results.items()
        if problems
    ]

    if storage_is_critical(payload):
        print("Freshness gate failed: storage critical.", file=sys.stderr)
        return 1
    if not fresh:
        print("Freshness gate failed: " + "; ".join(withheld) + ".", file=sys.stderr)
        return 1
    summary = f"Freshness gate passed for {', '.join(fresh)}."
    if withheld:
        summary += " Withheld by Securus: " + "; ".join(withheld) + "."
    print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

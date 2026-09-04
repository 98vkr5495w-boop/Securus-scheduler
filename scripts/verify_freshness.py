#!/usr/bin/env python3
"""Fail closed when decision-critical Securus sources are stale."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import sys
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


USER_AGENT = "Securus-Public-Watchdog/1.1"
DEFAULT_STATUS_ATTEMPTS = 3
DEFAULT_STATUS_TIMEOUT_SECONDS = 45


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


def load_status(
    base_url: str,
    *,
    attempts: int = DEFAULT_STATUS_ATTEMPTS,
    timeout_seconds: float = DEFAULT_STATUS_TIMEOUT_SECONDS,
    opener: Callable[..., Any] = urlopen,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, object]:
    """Fetch the public Securus status document, retrying transient failures.

    The status read is an idempotent GET, so a slow edge, a dropped connection,
    or a momentary 5xx must not fail a whole 30-minute cycle on its own. Client
    errors other than 429 are reported immediately because retrying them cannot
    help.
    """
    if attempts < 1:
        raise ValueError("attempts must be positive")
    url = f"{base_url.rstrip('/')}/api/data-sources"
    last_error: Exception | None = None
    for attempt in range(attempts):
        request = Request(
            url,
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
        )
        try:
            with opener(request, timeout=timeout_seconds) as response:
                payload = json.load(response)
            if not isinstance(payload, dict):
                raise RuntimeError("Securus status is not a JSON object")
            return payload
        except HTTPError as error:
            last_error = error
            if error.code < 500 and error.code != 429:
                raise RuntimeError(f"HTTP {error.code}") from error
        except (URLError, TimeoutError, ValueError, RuntimeError) as error:
            last_error = error
        if attempt + 1 < attempts:
            sleeper(min(20.0, 3.0 * (2**attempt)))
    raise RuntimeError(f"unavailable after {attempts} attempts: {last_error}")


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

    try:
        payload = load_status(args.url)
    except Exception as error:
        print(
            f"Freshness gate failed: Securus status is unavailable ({error}).",
            file=sys.stderr,
        )
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

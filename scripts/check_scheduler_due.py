#!/usr/bin/env python3
"""Dispatch the trusted Securus scheduler only when its canonical cycle is due."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode
from urllib.request import Request, urlopen


ApiGet = Callable[[str], dict[str, Any]]
COLLECT_JOB_NAME = "collect-inputs"
SCHEDULE_OFFSET_MINUTES = 7
REFRESH_MINUTES = 30
RETRY_COOLDOWN_MINUTES = 10
PUBLIC_SOURCE_URL = "https://edgelab-sports.jkv9c8bzjn.chatgpt.site/api/data-sources"
FREQUENT_SOURCES = frozenset({"mlb-stats-api", "action-network", "sleeper-nfl", "kalshi", "open-meteo", "climate"})
DEEP_SOURCES = frozenset({"nflverse", "baseball-savant"})


def parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.utcoffset() is not None else None


def cycle_start(now: datetime) -> datetime:
    """Return the UTC :07/:37 boundary containing ``now``."""
    current = now.astimezone(timezone.utc)
    shifted = current - timedelta(minutes=SCHEDULE_OFFSET_MINUTES)
    floored = shifted.replace(
        minute=30 if shifted.minute >= 30 else 0,
        second=0,
        microsecond=0,
    )
    return floored + timedelta(minutes=SCHEDULE_OFFSET_MINUTES)


def latest_slot(now: datetime, minute: int) -> datetime:
    """Return the most recent UTC occurrence of an explicit cron minute."""
    if minute not in (7, 37):
        raise ValueError("cycle minute must be 7 or 37")
    current = now.astimezone(timezone.utc)
    candidate = current.replace(minute=minute, second=0, microsecond=0)
    return candidate if candidate <= current else candidate - timedelta(hours=1)


def cycle_after_grace(now: datetime, grace_minutes: int) -> datetime:
    if grace_minutes < 0:
        raise ValueError("grace minutes must not be negative")
    return cycle_start(now - timedelta(minutes=grace_minutes))


def format_cycle_key(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def collection_timestamps(
    api_get: ApiGet,
    repository: str,
    workflow: str,
    branch: str,
) -> tuple[datetime | None, datetime | None]:
    query = urlencode({"branch": branch, "per_page": 20})
    payload = api_get(
        f"/repos/{repository}/actions/workflows/{workflow}/runs?{query}"
    )
    completions: list[datetime] = []
    attempts: list[datetime] = []
    for run in payload.get("workflow_runs", []):
        if not isinstance(run, dict) or not isinstance(run.get("id"), int):
            continue
        jobs = api_get(
            f"/repos/{repository}/actions/runs/{run['id']}/jobs?"
            + urlencode({"filter": "latest", "per_page": 100})
        )
        for job in jobs.get("jobs", []):
            if not isinstance(job, dict):
                continue
            if job.get("name") != COLLECT_JOB_NAME:
                continue
            started_at = parse_timestamp(job.get("started_at"))
            if started_at is not None and job.get("conclusion") != "skipped":
                attempts.append(started_at)
            completed_at = parse_timestamp(job.get("completed_at"))
            if completed_at is not None and job.get("conclusion") == "success":
                completions.append(completed_at)
    return max(attempts, default=None), max(completions, default=None)


def sources_due(payload: dict[str, Any], now: datetime, sources: frozenset[str], minutes: int) -> bool:
    rows = payload.get("sources")
    if not isinstance(rows, list):
        return True
    runs = {row.get("id"): row.get("lastRun") for row in rows if isinstance(row, dict)}
    for source in sources:
        run = runs.get(source)
        if not isinstance(run, dict) or run.get("status") != "SUCCEEDED":
            return True
        completed = parse_timestamp(run.get("completedAt"))
        if completed is None:
            return True
        age = now - completed
        if age < timedelta(0) or age >= timedelta(minutes=minutes):
            return True
    return False


def deep_refresh_due(payload: dict[str, Any], now: datetime) -> bool:
    # Preserve the six-hour deep cadence even when a delayed/skipped :07 slot
    # shifts collection to :37. Daily inputs must not starve behind fresh odds.
    return sources_due(payload, now, DEEP_SOURCES, 6 * 60)


def source_refresh_due(payload: dict[str, Any], now: datetime) -> bool:
    """Use completed feed timestamps, never a workflow or paper-scan receipt."""
    return sources_due(payload, now, FREQUENT_SOURCES, REFRESH_MINUTES) or deep_refresh_due(payload, now)


def read_public_source_status() -> dict[str, Any]:
    # Never send the GitHub credential to Securus or log this response body.
    request = Request(PUBLIC_SOURCE_URL, headers={
        "Accept": "application/json", "User-Agent": "Securus-Scheduler-Watchdog/2.0",
    })
    with urlopen(request, timeout=20) as response:
        if "application/json" not in response.headers.get("Content-Type", "").lower():
            raise ValueError("source status is not JSON")
        raw = response.read(524289)
    if len(raw) > 524288:
        raise ValueError("source status exceeded its size budget")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("source status is not an object")
    return payload


def effective_cycle(requested: datetime, now: datetime) -> datetime:
    """A delayed trigger must service now, not replay an already finished slot."""
    current = cycle_start(now)
    if requested > current:
        raise ValueError("cycle-key must not be in the future")
    return current


def scheduler_is_due(
    api_get: ApiGet,
    repository: str,
    workflow: str,
    branch: str,
    boundary: datetime,
    *,
    now: datetime | None = None,
    source_get: Callable[[], dict[str, Any]] | None = None,
) -> tuple[bool, str]:
    now = now or boundary
    attempted_at = completed_at = None
    github_error = False
    try:
        attempted_at, completed_at = collection_timestamps(
            api_get, repository, workflow, branch
        )
    except Exception:
        github_error = True
    if source_get is not None:
        try:
            if not source_refresh_due(source_get(), now):
                return False, "frequent feeds are under 30 minutes old and deep feeds are within their six-hour cadence"
            reason = "a feed is missing, failed, or due for its frequent/deep refresh cadence"
        except Exception:
            reason = "live source timestamps could not be verified"
        if github_error:
            # In particular, do not recursively dispatch on every completion
            # when both status services are unavailable and cooldown is unknown.
            return False, "collection history unavailable; recovery deferred to the next check, not confirmed healthy"
        if attempted_at is not None and timedelta(0) <= now - attempted_at < timedelta(minutes=RETRY_COOLDOWN_MINUTES):
            return False, "collection attempt is within the bounded 10-minute retry cooldown; readiness remains fail-closed"
        return True, reason
    if github_error:
        return True, "GitHub status could not be verified"
    if completed_at is None:
        return True, "no successful collect-inputs job was found"
    if now - completed_at >= timedelta(minutes=REFRESH_MINUTES):
        return True, "collection is older than the 30-minute refresh interval"
    boundary = boundary.astimezone(timezone.utc)
    if completed_at < boundary:
        return True, (
            f"last full collection predates cycle {format_cycle_key(boundary)}"
        )
    return False, (
        f"cycle {format_cycle_key(boundary)} completed at "
        f"{format_cycle_key(completed_at)}"
    )


def recover_cycle(
    api_get: ApiGet,
    api_request: Callable[..., dict[str, Any]],
    repository: str,
    workflow: str,
    branch: str,
    boundary: datetime,
    *,
    now: datetime | None = None,
    source_get: Callable[[], dict[str, Any]] | None = None,
) -> tuple[bool, str]:
    due, reason = scheduler_is_due(
        api_get, repository, workflow, branch, boundary, now=now, source_get=source_get
    )
    if due:
        api_request(
            f"/repos/{repository}/actions/workflows/{workflow}/dispatches",
            method="POST",
            payload={
                "ref": branch,
                "inputs": {
                    "recovery": "true",
                    "cycle_key": format_cycle_key(boundary),
                },
            },
        )
    return due, reason


class GitHubApi:
    def __init__(self, token: str):
        if not token:
            raise RuntimeError("GITHUB_TOKEN is unavailable")
        self.token = token

    def request(
        self,
        path: str,
        *,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = Request(
            f"https://api.github.com{path}",
            data=body,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "User-Agent": "Securus-Scheduler-Watchdog/1.0",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        with urlopen(request, timeout=30) as response:
            raw = response.read()
        return json.loads(raw) if raw else {}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--workflow", default="securus-scheduler.yml")
    parser.add_argument("--branch", default="main")
    parser.add_argument(
        "--cycle-key",
        help="Canonical UTC :07/:37 cycle boundary; defaults to the current cycle",
    )
    parser.add_argument("--cycle-slot-minute", type=int, choices=(7, 37))
    parser.add_argument("--grace-minutes", type=int, default=0)
    parser.add_argument("--github-output")
    parser.add_argument("--dispatch-if-due", action="store_true")
    args = parser.parse_args()
    if not args.repository:
        parser.error("repository is required")
    if args.grace_minutes < 0:
        parser.error("grace-minutes must not be negative")

    now = datetime.now(timezone.utc)
    if args.cycle_key:
        boundary = parse_timestamp(args.cycle_key)
        if boundary is None or boundary.minute not in (7, 37) or boundary.second != 0:
            parser.error("cycle-key must be a UTC :07 or :37 boundary")
        boundary = boundary.replace(microsecond=0)
    elif args.cycle_slot_minute is not None:
        boundary = latest_slot(now, args.cycle_slot_minute)
    else:
        boundary = cycle_after_grace(now, args.grace_minutes)
    try:
        boundary = effective_cycle(boundary, now)
    except ValueError as error:
        parser.error(str(error))
    cycle_key = format_cycle_key(boundary)

    api = GitHubApi(os.environ.get("GITHUB_TOKEN", ""))
    source_payload = None
    def source_get():
        nonlocal source_payload
        if source_payload is None:
            source_payload = read_public_source_status()
        return source_payload
    if args.dispatch_if_due:
        due, reason = recover_cycle(
            api.request,
            api.request,
            args.repository,
            args.workflow,
            args.branch,
            boundary,
            now=now,
            source_get=source_get,
        )
    else:
        due, reason = scheduler_is_due(
            api.request,
            args.repository,
            args.workflow,
            args.branch,
            boundary,
            now=now,
            source_get=source_get,
        )
    print(f"Scheduler cycle {cycle_key} due: {str(due).lower()} ({reason}).")
    if args.github_output:
        with Path(args.github_output).open("a", encoding="utf-8") as output:
            output.write(f"should_run={str(due).lower()}\n")
            output.write(f"cycle_key={cycle_key}\n")
            deep_due = source_payload is not None and deep_refresh_due(source_payload, now)
            output.write(f"deep_sources_due={str(deep_due).lower()}\n")
    if due and args.dispatch_if_due:
        print("Dispatched the existing trusted scheduler workflow.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

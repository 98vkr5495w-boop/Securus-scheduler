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
COLLECT_JOB_NAME = "collect-and-scan"
SCHEDULE_OFFSET_MINUTES = 7


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


def latest_successful_collection(
    api_get: ApiGet,
    repository: str,
    workflow: str,
    branch: str,
) -> datetime | None:
    query = urlencode({"branch": branch, "per_page": 20})
    payload = api_get(
        f"/repos/{repository}/actions/workflows/{workflow}/runs?{query}"
    )
    completions: list[datetime] = []
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
            if job.get("name") != COLLECT_JOB_NAME or job.get("conclusion") != "success":
                continue
            completed_at = parse_timestamp(job.get("completed_at"))
            if completed_at is not None:
                completions.append(completed_at)
    return max(completions, default=None)


def scheduler_is_due(
    api_get: ApiGet,
    repository: str,
    workflow: str,
    branch: str,
    boundary: datetime,
) -> tuple[bool, str]:
    try:
        completed_at = latest_successful_collection(
            api_get, repository, workflow, branch
        )
    except Exception:
        # Missing a refresh is worse than a duplicate request. The scheduler's
        # shared concurrency and deterministic run ID remain the final guards.
        return True, "GitHub status could not be verified"
    if completed_at is None:
        return True, "no successful collect-and-scan job was found"
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
) -> tuple[bool, str]:
    due, reason = scheduler_is_due(
        api_get, repository, workflow, branch, boundary
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

    if args.cycle_key:
        boundary = parse_timestamp(args.cycle_key)
        if boundary is None or boundary.minute not in (7, 37) or boundary.second != 0:
            parser.error("cycle-key must be a UTC :07 or :37 boundary")
        boundary = boundary.replace(microsecond=0)
    elif args.cycle_slot_minute is not None:
        boundary = latest_slot(datetime.now(timezone.utc), args.cycle_slot_minute)
    else:
        boundary = cycle_after_grace(datetime.now(timezone.utc), args.grace_minutes)
    cycle_key = format_cycle_key(boundary)

    api = GitHubApi(os.environ.get("GITHUB_TOKEN", ""))
    if args.dispatch_if_due:
        due, reason = recover_cycle(
            api.request,
            api.request,
            args.repository,
            args.workflow,
            args.branch,
            boundary,
        )
    else:
        due, reason = scheduler_is_due(
            api.request,
            args.repository,
            args.workflow,
            args.branch,
            boundary,
        )
    print(f"Scheduler cycle {cycle_key} due: {str(due).lower()} ({reason}).")
    if args.github_output:
        with Path(args.github_output).open("a", encoding="utf-8") as output:
            output.write(f"should_run={str(due).lower()}\n")
            output.write(f"cycle_key={cycle_key}\n")
    if due and args.dispatch_if_due:
        print("Dispatched the existing trusted scheduler workflow.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

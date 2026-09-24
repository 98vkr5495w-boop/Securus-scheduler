#!/usr/bin/env python3
"""Dispatch the trusted Securus scheduler only when its canonical cycle is due."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import math
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
# Securus projects a collector journal as abandoned 15 minutes after its start
# when no live lease remains, and unconditionally after 30 minutes. A RUNNING
# receipt older than this can never be a live worker; treating it as "in
# progress" would defer recovery forever (observed 2026-09-13, ~15 hours).
ABANDONED_COLLECTION_MINUTES = 20
PUBLIC_SOURCE_URL = "https://edgelab-sports.jkv9c8bzjn.chatgpt.site/api/data-sources"
FREQUENT_SOURCES = frozenset({"mlb-stats-api", "action-network", "sleeper-nfl", "kalshi", "open-meteo"})
DEEP_SOURCES = frozenset({"nflverse", "baseball-savant"})
GATED_SOURCES = FREQUENT_SOURCES | DEEP_SOURCES


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
    exclude_run_id: int | None = None,
) -> tuple[datetime | None, datetime | None]:
    query = urlencode({"branch": branch, "per_page": 20})
    payload = api_get(
        f"/repos/{repository}/actions/workflows/{workflow}/runs?{query}"
    )
    completions: list[datetime] = []
    attempts: list[datetime] = []
    runs = payload.get("workflow_runs")
    if not isinstance(runs, list):
        raise ValueError("collection history unavailable")
    for run in runs:
        if not isinstance(run, dict) or type(run.get("id")) is not int:
            raise ValueError("invalid workflow run")
        if run["id"] == exclude_run_id:
            continue
        if not isinstance(run.get("head_branch"), str) or not isinstance(run.get("event"), str):
            raise ValueError("workflow origin unavailable")
        if run.get("head_branch") != branch or run.get("event") not in ("schedule", "workflow_dispatch"):
            continue
        if run.get("status") not in ("completed", "queued", "in_progress", "waiting", "pending", "requested"):
            raise ValueError("workflow status unavailable")
        if run["status"] != "completed":
            # The cadence gate runs inside the shared, non-cancelling
            # ``securus-public-runtime`` concurrency group.  Once this run is
            # executing, a queued sibling cannot own collection; it is waiting
            # for this run to finish.  Treating that follower as active made a
            # delayed scheduled run and its watchdog recovery suppress each
            # other: the scheduled run skipped for PENDING, then the recovery
            # saw the scheduled run's completed maintenance inside cooldown.
            #
            # The external watchdog has no ``exclude_run_id`` and therefore
            # still blocks on every queued or active runtime before dispatch.
            # An in-progress sibling remains impossible under shared
            # concurrency and stays fail-closed if GitHub ever reports one.
            if (exclude_run_id is not None and
                    run["status"] in ("queued", "pending", "requested")):
                continue
            raise CollectionPending("runtime already queued or in progress")
        jobs = api_get(
            f"/repos/{repository}/actions/runs/{run['id']}/jobs?"
            + urlencode({"filter": "latest", "per_page": 100})
        )
        if not isinstance(jobs.get("jobs"), list) or jobs.get("total_count", len(jobs["jobs"])) > len(jobs["jobs"]):
            raise ValueError("incomplete collection job history")
        for job in jobs["jobs"]:
            if not isinstance(job, dict):
                continue
            if job.get("name") not in (COLLECT_JOB_NAME, "maintain-storage"):
                continue
            started_at = parse_timestamp(job.get("started_at"))
            if job.get("conclusion") != "skipped" and started_at is None:
                raise ValueError("collection timestamp unavailable")
            if started_at is not None and job.get("conclusion") != "skipped":
                attempts.append(started_at)
            completed_at = parse_timestamp(job.get("completed_at"))
            if job.get("name") == COLLECT_JOB_NAME and completed_at is not None and job.get("conclusion") == "success":
                completions.append(completed_at)
    return max(attempts, default=None), max(completions, default=None)


class CollectionPending(RuntimeError):
    pass


def storage_blocker(payload: dict[str, Any]) -> str | None:
    capacity = (payload.get("storage") or {}).get("capacity") or {}
    state, utilization = capacity.get("capacityState"), capacity.get("utilizationPercent")
    if state not in ("NORMAL", "WARNING", "CRITICAL") or type(utilization) not in (int, float) or not math.isfinite(utilization) or utilization < 0:
        return "live storage health unavailable; collection blocked"
    if state == "CRITICAL" or utilization >= 85 or capacity.get("maintenanceState") == "CRITICAL":
        return "storage critical; verified maintenance is required before collection"
    return None


def latest_storage_maintenance_at(payload: dict[str, Any]) -> datetime | None:
    """Return the newest independent maintenance timestamp exposed by the Site.

    GitHub job history already covers scheduler-owned maintenance. Counting the
    current scheduler run's intentional maintenance-before-collection step here
    would make every guarded recovery defer itself. The Site's independent
    receipt closes only the external Cloudflare visibility gap.
    """
    storage = payload.get("storage") or {}
    assurance = storage.get("assurance") or {}
    independent = parse_timestamp(assurance.get("lastIndependentMaintenanceAt"))
    capacity = storage.get("capacity") or {}
    archives = capacity.get("archives") or {}
    last_maintenance = archives.get("lastMaintenance") or {}
    trigger_name = last_maintenance.get("triggerName")

    # The Site can project lastIndependentMaintenanceAt from its newest
    # maintenance receipt even when that receipt belongs to this scheduler.
    # Prefer the explicit receipt identity whenever it is available. GitHub
    # job history separately cools down scheduler-owned maintenance, while a
    # cadence gate excludes only its own current run so maintenance followed
    # intentionally by collection cannot defer itself.
    if isinstance(trigger_name, str) and trigger_name:
        if trigger_name != "cloudflare-cron-10m":
            return None
        candidates = [
            independent,
            parse_timestamp(last_maintenance.get("startedAt")),
            parse_timestamp(last_maintenance.get("completedAt")),
        ]
        return max((value for value in candidates if value is not None), default=None)

    # Older Site projections may omit receipt identity. In that case the
    # dedicated independent timestamp remains the only bounded evidence.
    return independent


def storage_maintenance_in_cooldown(payload: dict[str, Any], now: datetime) -> bool:
    latest = latest_storage_maintenance_at(payload)
    return latest is not None and now - latest < timedelta(minutes=RETRY_COOLDOWN_MINUTES)


def collection_in_progress(payload: dict[str, Any], now: datetime | None = None) -> bool:
    """True only for a RUNNING receipt young enough to still be a live worker.

    A receipt without a parseable start, or one older than the abandonment
    window, is a dead journal: it must not suppress recovery. Securus's own
    lease rejects a genuinely overlapping collection with HTTP 409.
    """
    now = now or datetime.now(timezone.utc)
    for row in payload.get("sources", []) or []:
        run = row.get("lastRun") if isinstance(row, dict) else None
        if not isinstance(run, dict) or run.get("status") not in ("RUNNING", "QUEUED", "IN_PROGRESS"):
            continue
        # Only the feeds this gate depends on can defer it. Bookkeeping rows
        # such as the Site's own recovery marker are not collection evidence.
        if row.get("id") not in GATED_SOURCES:
            continue
        started = parse_timestamp(run.get("startedAt"))
        if started is None:
            continue
        if timedelta(0) <= now - started < timedelta(minutes=ABANDONED_COLLECTION_MINUTES):
            return True
    return False


def abandoned_collections(payload: dict[str, Any], now: datetime | None = None) -> list[str]:
    """Source IDs whose latest receipt is RUNNING but too old or unverifiable to be live."""
    now = now or datetime.now(timezone.utc)
    abandoned = []
    for row in payload.get("sources", []) or []:
        run = row.get("lastRun") if isinstance(row, dict) else None
        if not isinstance(run, dict) or run.get("status") not in ("RUNNING", "QUEUED", "IN_PROGRESS"):
            continue
        if row.get("id") not in GATED_SOURCES:
            continue
        started = parse_timestamp(run.get("startedAt"))
        if started is None or not timedelta(0) <= now - started < timedelta(minutes=ABANDONED_COLLECTION_MINUTES):
            abandoned.append(str(row.get("id")))
    return sorted(abandoned)


def gate_state(due: bool, reason: str) -> str:
    """Classify a gate outcome so a skipped cycle cannot masquerade as healthy.

    DUE: collection dispatches or proceeds. FRESH: nothing is due. PENDING:
    another trusted runtime already owns this cycle. DEFERRED: feeds are stale
    or unverifiable and nothing will refresh them in this run.
    """
    if due:
        return "DUE"
    if reason.startswith("frequent feeds are under") or reason.startswith("cycle ") and "completed at" in reason:
        return "FRESH"
    if "already queued or in progress" in reason:
        return "PENDING"
    return "DEFERRED"


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
    return (frequent_refresh_due(payload, now)
            or deep_refresh_due(payload, now) or paper_scan_due(payload, now))


def frequent_refresh_due(payload: dict[str, Any], now: datetime) -> bool:
    """True when a decision-critical frequent feed needs a real refresh."""
    return sources_due(payload, now, FREQUENT_SOURCES, REFRESH_MINUTES)


def paper_scan_due(payload: dict[str, Any], now: datetime) -> bool:
    scan = payload.get("lastPaperScan")
    if not isinstance(scan, dict) or scan.get("mode") != "PAPER_ONLY" or not scan.get("runId"):
        return True
    completed = parse_timestamp(scan.get("completedAt"))
    return completed is None or not timedelta(0) <= now - completed < timedelta(minutes=60)


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
    exclude_run_id: int | None = None,
) -> tuple[bool, str]:
    now = now or boundary
    attempted_at = completed_at = None
    github_error = False
    try:
        attempted_at, completed_at = collection_timestamps(
            api_get, repository, workflow, branch, exclude_run_id
        )
    except CollectionPending as error:
        return False, str(error)
    except Exception:
        github_error = True
    if source_get is not None:
        try:
            payload = source_get()
            if collection_in_progress(payload, now):
                return False, "a runtime source collection is unfinished; recovery deferred"
            abandoned = abandoned_collections(payload, now)
            blocker = storage_blocker(payload)
            if blocker:
                return False, blocker
            if abandoned:
                reason = ("an abandoned collector receipt is older than the "
                          f"{ABANDONED_COLLECTION_MINUTES}-minute lease horizon ({', '.join(abandoned)}); "
                          "fresh collection is required")
            elif sources_due(payload, now, FREQUENT_SOURCES, REFRESH_MINUTES):
                reason = "a frequent feed is missing, failed, or due for its 30-minute refresh"
            elif deep_refresh_due(payload, now):
                reason = "a deep-stat feed is missing, failed, or due for its six-hour refresh"
            elif paper_scan_due(payload, now):
                reason = "a completed paper scan is missing or older than 60 minutes; full guarded recovery is due"
            else:
                return False, "frequent feeds are under 30 minutes old and deep feeds are within their six-hour cadence"
        except Exception:
            return False, "live source timestamps and storage could not be verified; recovery deferred, not confirmed healthy"
        if storage_maintenance_in_cooldown(payload, now):
            return False, f"{reason}; storage maintenance is within the bounded 10-minute retry cooldown; readiness remains fail-closed"
        if github_error:
            # In particular, do not recursively dispatch on every completion
            # when both status services are unavailable and cooldown is unknown.
            return False, "collection history unavailable; recovery deferred to the next check, not confirmed healthy"
        if attempted_at is not None and now - attempted_at < timedelta(minutes=RETRY_COOLDOWN_MINUTES):
            return False, f"{reason}; collection attempt is within the bounded 10-minute retry cooldown; readiness remains fail-closed"
        return True, reason
    if github_error:
        return False, "GitHub status could not be verified; recovery deferred, not confirmed healthy"
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
    maintenance_only = False
    # Also drain WARNING storage while feeds are fresh: a green feed check must
    # not starve preventive cleanup. Overdue feeds retain priority below the
    # critical boundary. Neither path changes collection cadence or admission.
    if not due and source_get is not None:
        try:
            payload = source_get()
            blocker = storage_blocker(payload)
            capacity = (payload.get("storage") or {}).get("capacity") or {}
            critical = reason.startswith("storage critical;") and blocker == reason
            warning = (blocker is None and capacity.get("capacityState") == "WARNING"
                       and 75 <= capacity.get("utilizationPercent", -1) < 85
                       and not source_refresh_due(payload, now or boundary))
            if critical or warning:
                # Recheck actual job history even if the first gate returned
                # early for fresh feeds, unknown history or a pending runtime.
                attempted, _ = collection_timestamps(api_get, repository, workflow, branch)
                if (not collection_in_progress(payload, now or boundary) and
                    not storage_maintenance_in_cooldown(payload, now or boundary) and
                    (attempted is None or (now or boundary) - attempted >= timedelta(minutes=RETRY_COOLDOWN_MINUTES))):
                    due = maintenance_only = True
                    level = "critical" if critical else "warning"
                    reason = f"storage {level}; dispatching verified maintenance only, with collection and scan disabled"
        except Exception:
            return False, "storage maintenance prerequisites unavailable; maintenance dispatch deferred, not confirmed healthy"
    if due:
        inputs = {"recovery": "true", "cycle_key": format_cycle_key(boundary)}
        if maintenance_only:
            inputs["maintenance_only"] = "true"
        api_request(
            f"/repos/{repository}/actions/workflows/{workflow}/dispatches",
            method="POST",
            payload={
                "ref": branch,
                "inputs": inputs,
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
            exclude_run_id=int(os.environ["GITHUB_RUN_ID"]) if os.environ.get("GITHUB_RUN_ID") else None,
        )
    state = gate_state(due, reason)
    print(f"Scheduler cycle {cycle_key} due: {str(due).lower()} ({reason}). Gate state: {state}.")
    if state == "DEFERRED":
        # The cadence workflow consumes gate_state and fails its own explicit
        # sentinel step. The standalone watchdog has no such caller: its CLI
        # must fail too, not turn a warned-but-deferred recovery into green CI.
        severity = "error" if args.dispatch_if_due else "warning"
        print(f"::{severity}::Scheduler cycle {cycle_key} was deferred without refreshing feeds: {reason}.")
    if args.github_output:
        with Path(args.github_output).open("a", encoding="utf-8") as output:
            output.write(f"should_run={str(due).lower()}\n")
            output.write(f"cycle_key={cycle_key}\n")
            output.write(f"gate_state={state}\n")
            frequent_due = source_payload is not None and frequent_refresh_due(source_payload, now)
            deep_due = source_payload is not None and deep_refresh_due(source_payload, now)
            output.write(f"frequent_sources_due={str(frequent_due).lower()}\n")
            output.write(f"deep_sources_due={str(deep_due).lower()}\n")
    if due and args.dispatch_if_due:
        print("Dispatched the existing trusted scheduler workflow.")
    return 1 if args.dispatch_if_due and state == "DEFERRED" else 0


if __name__ == "__main__":
    raise SystemExit(main())

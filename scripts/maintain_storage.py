#!/usr/bin/env python3
"""Wait for verified archival and reserve collection headroom; never collect."""
from __future__ import annotations

import argparse
import json
import math
import os
import time
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener

SITE = "https://edgelab-sports.jkv9c8bzjn.chatgpt.site"
MAX_PASSES = 12
TIME_BUDGET_SECONDS = 240


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("Authenticated maintenance redirects are forbidden")


def read_json(request):
    with build_opener(NoRedirect).open(request, timeout=20) as response:
        if "application/json" not in response.headers.get("Content-Type", "").lower():
            raise RuntimeError("Maintenance returned non-JSON data")
        raw = response.read(524289)
    if len(raw) > 524288:
        raise RuntimeError("Maintenance response exceeded the size budget")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise RuntimeError("Maintenance response must be an object")
    return value


def identity():
    url = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL", "")
    token = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "")
    if not url or not token:
        raise RuntimeError("The trusted short-lived GitHub identity is unavailable")
    value = read_json(Request(url + "&audience=securus-collector",
        headers={"Authorization": f"bearer {token}"})).get("value")
    if not isinstance(value, str) or not value:
        raise RuntimeError("GitHub did not return a short-lived identity")
    print(f"::add-mask::{value}", flush=True)
    return value


def request(path, payload=None):
    headers = {"Accept": "application/json", "Cache-Control": "no-cache",
               "User-Agent": "Securus-Verified-Maintenance/1.0"}
    if path != "/api/data-sources":
        headers["Authorization"] = f"Bearer {identity()}"
    data = None if payload is None else json.dumps(payload).encode()
    if data is not None:
        headers["Content-Type"] = "application/json"
    return read_json(Request(SITE + path, data=data, headers=headers,
                             method="GET" if data is None else "POST"))


def capacity(value):
    if not isinstance(value, dict):
        raise RuntimeError("Live storage health is unavailable")
    state, percent = value.get("capacityState"), value.get("utilizationPercent")
    actual, limit = value.get("actualBytes"), value.get("safetyCapacityBytes")
    numbers = (percent, actual, limit)
    if (state not in ("NORMAL", "WARNING", "CRITICAL") or
        any(type(n) not in (int, float) or not math.isfinite(n) for n in numbers) or
        percent < 0 or actual < 0 or limit <= 0 or abs(percent - 100 * actual / limit) > .02):
        raise RuntimeError("Live storage health is invalid or inconsistent")
    return state, percent, actual


def maintain(api=request, *, target=83.5, max_passes=MAX_PASSES,
             sleep=time.sleep, clock=time.monotonic):
    if not 70 <= target <= 84 or not 1 <= max_passes <= MAX_PASSES:
        raise ValueError("Maintenance bounds are invalid")
    deadline = clock() + TIME_BUDGET_SECONDS
    initial = api("/api/data-sources")
    _, _, previous_bytes = capacity((initial.get("storage") or {}).get("capacity"))
    stagnant = 0
    for attempt in range(max_passes):
        if clock() >= deadline:
            break
        for admission in range(4):
            if clock() >= deadline:
                raise RuntimeError("Maintenance admission is pending; collection blocked")
            accepted = api("/api/storage-maintenance", {
                "triggerName": "public-github-actions-verified", "maxArchives": 12,
            })
            run_id = accepted.get("runId")
            if accepted.get("accepted") is True and type(run_id) is int and run_id > 0:
                break
            # A post-ingest worker can finish its journal just before releasing
            # its lease, or hold the lease before inserting the journal. Retry
            # only that explicit handoff, still inside the original time budget.
            if (accepted.get("accepted") is True and accepted.get("alreadyRunning") is True
                and accepted.get("status") == "RUNNING" and run_id is None and admission < 3):
                sleep(2)
                continue
            raise RuntimeError("Maintenance was not accepted with a durable run ID")
        # POST.after is explicitly a BEFORE measurement. Never trust it or a
        # server-supplied status URL. Poll this exact run on the pinned Site.
        for _ in range(24):
            if clock() >= deadline:
                raise RuntimeError(f"Maintenance #{run_id} is pending; collection blocked")
            result = api(f"/api/storage-maintenance-status?runId={run_id}")
            if result.get("runId") != run_id:
                raise RuntimeError("Maintenance status identity mismatch")
            state = result.get("status")
            if state == "SUCCEEDED":
                if not result.get("completedAt") or result.get("error"):
                    raise RuntimeError(f"Maintenance #{run_id} has invalid completion evidence")
                break
            if state != "RUNNING":
                raise RuntimeError(f"Maintenance #{run_id} failed ({state}); collection blocked")
            sleep(4)
        else:
            raise RuntimeError(f"Maintenance #{run_id} is pending; collection blocked")
        state, percent, actual = capacity(result.get("capacity"))
        rows_deleted = result.get("rowsDeleted")
        deleted = rows_deleted if type(rows_deleted) is int and rows_deleted >= 0 else 0
        print(f"Verified maintenance #{run_id}: {percent:.2f}% storage, {deleted} rows deleted "
              f"(pass {attempt + 1}/{max_passes}).", flush=True)
        if state != "CRITICAL" and percent <= target:
            return result
        # Concurrent ingestion can hold the measured size flat while a pass
        # genuinely deletes rows; judge stagnation by the run's own verified
        # deletions first, and by measured bytes only when it deleted nothing.
        stagnant = 0 if deleted > 0 else stagnant + 1 if actual >= previous_bytes else 0
        if stagnant >= 3:
            raise RuntimeError("Three verified maintenance passes made no capacity progress; collection blocked")
        previous_bytes = actual
    raise RuntimeError(f"Bounded maintenance did not reach {target}% headroom; collection blocked")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-percent", type=float, default=83.5)
    args = parser.parse_args()
    try:
        maintain(target=args.target_percent)
    except HTTPError as error:
        print(f"::error::Verified storage maintenance HTTP {error.code}; collection blocked")
        return 1
    except Exception as error:
        print(f"::error::{error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

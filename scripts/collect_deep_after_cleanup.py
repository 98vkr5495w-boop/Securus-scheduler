"""Run due deep collectors behind the verified paper scan and measured headroom."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

from maintain_storage import SITE, capacity

SOURCES = ("nflverse", "baseball-savant")
SLOT_WIDTH = timedelta(minutes=30)
READ_MAX_AGE = timedelta(seconds=60)
READ_TIMEOUT = 12
CACHE_WAIT = 31
POST_BUDGET = timedelta(seconds=125)
DRAIN_BUDGET = 130


def utcnow():
    return datetime.now(timezone.utc)


def parse_time(value):
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("UTC timestamp required")
    return datetime.fromisoformat(value[:-1] + "+00:00")


def active_slot(now):
    minute = 37 if now.minute >= 37 else 7 if now.minute >= 7 else 37
    hour = now - timedelta(hours=1) if now.minute < 7 else now
    return hour.replace(minute=minute, second=0, microsecond=0)


def in_slot(start, now, reserve=POST_BUDGET):
    return start <= now and now + reserve < start + SLOT_WIDTH


def read_status():
    result = subprocess.run(
        ["curl", "--fail", "--silent", "--max-time", str(READ_TIMEOUT),
         "--max-filesize", "524288", "--header", "Accept: application/json",
         "--header", "Cache-Control: no-cache", SITE + "/api/data-sources"],
        capture_output=True, check=True, timeout=READ_TIMEOUT + 1,
    )
    payload = json.loads(result.stdout)
    if not isinstance(payload, dict):
        raise ValueError("Expected status object")
    return payload


def invoke(argv, timeout):
    # curl's hard timeout bounds the wire call; Python bounds the shell wrapper.
    return subprocess.run(argv, timeout=timeout, check=False).returncode


def measurement(payload, now, not_before):
    cap = (payload.get("storage") or {}).get("capacity") or {}
    rdy = payload.get("paperBetReadiness") or {}
    checked = parse_time(rdy.get("checkedAt"))
    state, pct, actual = capacity(cap)
    limit = cap.get("safetyCapacityBytes")
    if (cap.get("sizeSource") != "D1_META" or type(actual) is not int
            or type(limit) is not int or not 0 < actual
            or cap.get("maintenanceState") not in {"NORMAL", "WARNING", "CRITICAL"}):
        raise ValueError("No coherent measured D1 capacity")
    if rdy.get("snapshotFreshness") == "STALE" or not (
        max(now - READ_MAX_AGE, not_before) <= checked <= now
    ):
        return "WAIT_FOR_FRESH_VIEW"
    if state == "CRITICAL" or cap.get("maintenanceState") == "CRITICAL" or pct > 84 or actual * 100 > limit * 84:
        return "NEEDS_CLEANUP"
    return "ADMITTED"


def already_fresh(payload, source, now):
    matches = [r for r in payload.get("sources", []) if isinstance(r, dict) and r.get("id") == source]
    if len(matches) != 1:
        return False
    receipt = matches[0].get("lastRun")
    if not isinstance(receipt, dict) or receipt.get("status") != "SUCCEEDED":
        return False
    try:
        age = now - parse_time(receipt.get("completedAt"))
    except ValueError:
        return False
    # Same individual six-hour cadence used by the existing due gate.
    return timedelta(0) <= age < timedelta(hours=6)


def run(slot, verified_at, *, manual=False, api=read_status, now=utcnow,
        call=invoke, sleep=time.sleep):
    try:
        verified = parse_time(verified_at)
        if manual and not slot:
            start = active_slot(verified)
        elif re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:(07|37):00Z", slot):
            start = parse_time(slot)
        else:
            raise ValueError
        if not start <= verified <= now() < start + SLOT_WIDTH:
            raise ValueError
    except ValueError:
        print("::warning::Deep collection deferred: no verified scan in the current slot.")
        return 1

    not_before = verified
    drained = False
    failed = False
    errors = (OSError, ValueError, TypeError, RuntimeError, AttributeError, subprocess.SubprocessError)

    def probe():
        if not in_slot(start, now(), POST_BUDGET + timedelta(seconds=READ_TIMEOUT + 1)):
            return None, "DEFER"
        try:
            payload = api()
            checked = now()
            return payload, measurement(payload, checked, not_before)
        except errors:
            return None, "DEFER"

    def wait_for_fresh_view(payload, decision):
        if decision == "WAIT_FOR_FRESH_VIEW" and in_slot(
            start, now(), POST_BUDGET + timedelta(seconds=CACHE_WAIT + READ_TIMEOUT + 1)
        ):
            sleep(CACHE_WAIT)
            return probe()
        return payload, decision

    for source in SOURCES:
        payload, decision = wait_for_fresh_view(*probe())
        if decision in {"ADMITTED", "NEEDS_CLEANUP"} and not manual and already_fresh(payload, source, now()):
            print(f"{source}: durable refresh is within the six-hour cadence.")
            continue
        if not drained and decision == "NEEDS_CLEANUP" and in_slot(
            start, now(), POST_BUDGET + timedelta(seconds=DRAIN_BUDGET + CACHE_WAIT + READ_TIMEOUT + 1)
        ):
            drained = True
            try:
                code = call([sys.executable, "scripts/maintain_storage.py",
                             "--target-percent", "83.5", "--max-passes", "3"], DRAIN_BUDGET)
            except errors:
                code = 1
            not_before = now()
            if not code:
                payload, decision = wait_for_fresh_view(*probe())
            else:
                # Do not reuse a possibly pre-maintenance snapshot after an
                # ambiguous response; the pending handoff remains authoritative.
                print("::warning::Deep-source cleanup did not verify; no additional collection was attempted.")
                return 1
        if decision != "ADMITTED" or not in_slot(start, now()):
            print(f"::warning::{source}: deferred without a fresh measured admission in this slot.")
            failed = True
            continue
        try:
            code = call(["bash", "scripts/collect_source.sh", f"mode=daily&source={source}"], 122)
        except errors:
            code = 1
        not_before = now()
        if code:
            print(f"::warning::{source}: collection was not verified; not retried.")
            # The failed transport may still have a producer-side lease or a
            # committed generation. Do not send a second mutating request.
            return 1
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(run(os.getenv("CYCLE_KEY", ""), os.getenv("PAPER_SCAN_VERIFIED_AT", ""),
                         manual=os.getenv("MANUAL_DEEP") == "true"))

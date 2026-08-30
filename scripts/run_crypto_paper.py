#!/usr/bin/env python3
"""Run and verify one durable Securus Crypto paper scan.

The caller creates one UUIDv4 request ID and never starts a replacement scan.
If the POST response is lost, the workflow verifies the same journaled run via
the authenticated read-only status endpoint.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
import sys
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import uuid


USER_AGENT = "Securus-Crypto-Paper-Runner/2.0"
DEFAULT_POLL_ATTEMPTS = 60
DEFAULT_POLL_DELAY_SECONDS = 5
NONTERMINAL_STATUSES = {"QUEUED", "RUNNING"}
TERMINAL_FAILURE_STATUSES = {"FAILED", "BLOCKED", "TIMED_OUT"}


def securus_oidc_token() -> str:
    request_url = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL", "")
    request_token = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "")
    if request_url and request_token:
        separator = "&" if "?" in request_url else "?"
        request = Request(
            f"{request_url}{separator}audience=securus-collector",
            headers={"Authorization": f"bearer {request_token}"},
        )
        with urlopen(request, timeout=30) as response:
            payload = json.load(response)
        token = str(payload.get("value") or "")
        if not token:
            raise RuntimeError("GitHub OIDC response did not include a token")
        print(f"::add-mask::{token}")
        os.environ["SECURUS_OIDC_TOKEN"] = token
        return token
    token = os.environ.get("SECURUS_OIDC_TOKEN", "")
    if not token:
        raise RuntimeError("Securus OIDC identity is unavailable")
    return token


def request_json(
    url: str,
    *,
    method: str = "GET",
    authenticated: bool = False,
    payload: dict[str, Any] | None = None,
    attempts: int = 3,
    timeout_seconds: float = 120,
) -> dict[str, Any]:
    if method != "GET" and attempts != 1:
        raise ValueError("non-GET requests must use exactly one transport attempt")
    last_error: Exception | None = None
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    for attempt in range(attempts):
        headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        if authenticated:
            # Refresh identity for every request. GitHub OIDC credentials can
            # expire during a long collector workflow.
            headers["Authorization"] = f"Bearer {securus_oidc_token()}"
        try:
            request = Request(url, data=body, method=method, headers=headers)
            with urlopen(request, timeout=timeout_seconds) as response:
                response_payload = json.load(response)
            if not isinstance(response_payload, dict):
                raise RuntimeError("Securus returned a non-object JSON response")
            return response_payload
        except HTTPError as error:
            last_error = error
            if error.code < 500 and error.code != 429:
                detail = error.read().decode("utf-8", errors="replace")[:400]
                raise RuntimeError(f"HTTP {error.code}: {detail}") from error
        except (URLError, TimeoutError) as error:
            last_error = error
        if attempt + 1 < attempts:
            time.sleep(min(20, 3 * (2**attempt)))
    raise RuntimeError(f"request failed after {attempts} attempts: {last_error}")


def parse_timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"Crypto paper run verification failed: {field}={value!r}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise RuntimeError(
            f"Crypto paper run verification failed: {field}={value!r}"
        ) from error
    if parsed.utcoffset() is None:
        raise RuntimeError(
            f"Crypto paper run verification failed: {field} lacks a timezone"
        )
    return parsed


def validate_lifecycle(
    payload: dict[str, Any],
    run_id: str,
    trigger_name: str,
    expected_watermark: dict[str, Any] | None,
) -> tuple[str, dict[str, Any]]:
    mismatches = []
    if payload.get("accepted") is not True:
        mismatches.append(f"accepted={payload.get('accepted')!r}")
    if payload.get("runId") != run_id:
        mismatches.append(f"runId={payload.get('runId')!r}")
    if payload.get("triggerName") != trigger_name:
        mismatches.append(f"triggerName={payload.get('triggerName')!r}")
    if payload.get("mode") != "PAPER_ONLY":
        mismatches.append(f"mode={payload.get('mode')!r}")
    status = payload.get("status")
    if not isinstance(status, str):
        mismatches.append(f"status={status!r}")
    elif status not in NONTERMINAL_STATUSES | TERMINAL_FAILURE_STATUSES | {"SUCCEEDED"}:
        mismatches.append(f"status={status!r}")
    watermark = payload.get("sourceWatermark")
    if not isinstance(watermark, dict) or watermark.get("version") != 1:
        mismatches.append(f"sourceWatermark={watermark!r}")
        watermark = {}
    elif expected_watermark is not None and watermark != expected_watermark:
        mismatches.append("sourceWatermark changed while the run was in progress")
    elif any(
        isinstance(watermark.get(field), bool)
        or not isinstance(watermark.get(field), int)
        or watermark.get(field, -1) < 0
        for field in (
            "sourceSyncRunId",
            "oddsSnapshotId",
            "playerPropSnapshotId",
            "statSnapshotId",
        )
    ):
        mismatches.append(f"sourceWatermark counters={watermark!r}")
    if mismatches:
        raise RuntimeError("Crypto paper run verification failed: " + ", ".join(mismatches))
    return status, watermark


def validate_completed_scan(payload: dict[str, Any]) -> list[dict[str, Any]]:
    expected = {
        "accepted": True,
        "mode": "PAPER_ONLY",
        "analyst": "SECURUS",
        "paperBettor": "CRYPTO",
        "venue": "KALSHI",
        "realMoneyExecution": False,
    }
    mismatches = [
        f"result.{field}={payload.get(field)!r}"
        for field, value in expected.items()
        if payload.get(field) != value
    ]
    if payload.get("status") in NONTERMINAL_STATUSES | {"ALREADY_RUNNING"}:
        mismatches.append(f"result.status={payload.get('status')!r}")
    errors = payload.get("errors")
    if not isinstance(errors, list) or errors:
        mismatches.append(f"result.errors={errors!r}")
    decisions = payload.get("decisions")
    if not isinstance(decisions, list) or any(not isinstance(row, dict) for row in decisions):
        mismatches.append(f"result.decisions={decisions!r}")
        decisions = []
    if mismatches:
        raise RuntimeError("Crypto paper scan verification failed: " + ", ".join(mismatches))
    return decisions


def validate_terminal_run(
    lifecycle: dict[str, Any],
    run_id: str,
    trigger_name: str,
    expected_watermark: dict[str, Any] | None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    status, watermark = validate_lifecycle(
        lifecycle,
        run_id,
        trigger_name,
        expected_watermark,
    )
    if status in TERMINAL_FAILURE_STATUSES:
        raise RuntimeError(
            f"Crypto paper run {status}: {lifecycle.get('error') or 'no error detail'}"
        )
    if status != "SUCCEEDED":
        raise RuntimeError(f"Crypto paper run is not terminal: status={status!r}")
    requested_at = parse_timestamp(lifecycle.get("requestedAt"), "requestedAt")
    started_at = parse_timestamp(lifecycle.get("startedAt"), "startedAt")
    completed_at = parse_timestamp(lifecycle.get("completedAt"), "completedAt")
    captured_at = parse_timestamp(watermark.get("capturedAt"), "sourceWatermark.capturedAt")
    if not requested_at <= captured_at <= started_at:
        raise RuntimeError("Crypto paper run source watermark timestamps are out of order")
    if completed_at < started_at:
        raise RuntimeError("Crypto paper run completed before it started")
    result = lifecycle.get("result")
    if not isinstance(result, dict):
        raise RuntimeError(f"Crypto paper scan verification failed: result={result!r}")
    result_started_at = parse_timestamp(result.get("startedAt"), "result.startedAt")
    result_completed_at = parse_timestamp(result.get("completedAt"), "result.completedAt")
    if not started_at <= result_started_at <= result_completed_at <= completed_at:
        raise RuntimeError("Crypto paper run result timestamps are out of order")
    return watermark, validate_completed_scan(result)


def run_and_verify(
    base_url: str,
    *,
    poll_attempts: int = DEFAULT_POLL_ATTEMPTS,
    poll_delay_seconds: float = DEFAULT_POLL_DELAY_SECONDS,
    run_id: str | None = None,
    loader: Callable[..., dict[str, Any]] = request_json,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    if poll_attempts < 1:
        raise ValueError("poll_attempts must be positive")
    run_id = run_id or str(uuid.uuid4())
    root = base_url.rstrip("/")
    trigger_name = "github-actions-paper-scan"
    expected_watermark: dict[str, Any] | None = None
    lifecycle: dict[str, Any] | None = None
    post_error: Exception | None = None

    try:
        lifecycle = loader(
            f"{root}/api/paper-run-tracked",
            method="POST",
            authenticated=True,
            payload={"requestId": run_id, "triggerName": trigger_name},
            attempts=1,
            timeout_seconds=180,
        )
    except Exception as error:
        # The server may have committed and completed the journaled run even if
        # the edge or client lost the POST response. Never replay the POST.
        post_error = error
        lifecycle = None
        print(
            f"Crypto paper POST response unavailable; polling run {run_id}: {error}",
            file=sys.stderr,
        )
    else:
        status, expected_watermark = validate_lifecycle(
            lifecycle,
            run_id,
            trigger_name,
            None,
        )
        if not isinstance(lifecycle.get("created"), bool):
            raise RuntimeError(
                "Crypto paper run verification failed: "
                f"created={lifecycle.get('created')!r}"
            )

    polls = 0
    last_poll_error: Exception | None = None
    status_url = f"{root}/api/paper-run-status?{urlencode({'runId': run_id})}"
    for attempt in range(1, poll_attempts + 1):
        polls = attempt
        try:
            candidate = loader(
                status_url,
                authenticated=True,
                attempts=1,
                timeout_seconds=30,
            )
        except Exception as error:
            last_poll_error = error
        else:
            status, watermark = validate_lifecycle(
                candidate,
                run_id,
                trigger_name,
                expected_watermark,
            )
            if expected_watermark is None:
                expected_watermark = watermark
            lifecycle = candidate
            last_poll_error = None
            if status not in NONTERMINAL_STATUSES:
                break
        if attempt < poll_attempts:
            sleeper(poll_delay_seconds)
    else:
        detail = last_poll_error or post_error or "run remained nonterminal"
        raise RuntimeError(
            f"Crypto paper run {run_id} was not verified after "
            f"{poll_attempts} status checks: {detail}"
        )

    assert lifecycle is not None
    watermark, decisions = validate_terminal_run(
        lifecycle,
        run_id,
        trigger_name,
        expected_watermark,
    )
    # Public workflow logs intentionally expose no source watermark, market,
    # recommendation, or decision-reason details.
    return {
        "status": "VERIFIED",
        "mode": "PAPER_ONLY",
        "runId": run_id,
        "postResponseLost": post_error is not None,
        "statusPolls": polls,
        "decisionCount": len(decisions),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("--poll-attempts", type=int, default=DEFAULT_POLL_ATTEMPTS)
    parser.add_argument(
        "--poll-delay-seconds",
        type=float,
        default=DEFAULT_POLL_DELAY_SECONDS,
    )
    args = parser.parse_args()
    try:
        report = run_and_verify(
            args.url,
            poll_attempts=args.poll_attempts,
            poll_delay_seconds=args.poll_delay_seconds,
        )
    except Exception:
        # The public scheduler log is deliberately non-diagnostic. Detailed
        # lifecycle and decision evidence stays inside private Securus telemetry.
        print("Securus paper scan failed verification.", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

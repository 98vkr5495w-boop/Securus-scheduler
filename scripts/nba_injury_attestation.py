#!/usr/bin/env python3
"""Independently verify one NBA injury-report PDF and submit bounded evidence.

The three CLI modes are intentionally suitable for separate GitHub Actions jobs:

* ``challenge`` uses the Securus OIDC identity to obtain the exact report target.
* ``verify`` has no Securus identity, downloads those exact public bytes, and runs
  pdftotext without exposing extracted injury text as a workflow output.
* ``submit`` uses a fresh Securus OIDC identity to post only bounded findings.

Securus remains authoritative for NBA readiness.  This client never sends a
``complete`` decision and never treats a different PDF digest as equivalent.
"""

from __future__ import annotations

import argparse
import base64
from datetime import date, datetime
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Any, Callable
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import (
    HTTPRedirectHandler,
    Request,
    build_opener,
)
from zoneinfo import ZoneInfo


ATTESTATION_PATH = "/api/nba-injury-attestation"
PARSER_ID = "poppler-pdftotext-layout-v1"
SCHEMA_VERSION = 2
VERIFIER_VERSION = "securus-nba-completeness-v2"
PDF_HOST = "ak-static.cms.nba.com"
MAX_JSON_BYTES = 64 * 1024
MIN_PDF_BYTES = 8 * 1024
MAX_PDF_BYTES = 5 * 1024 * 1024
MAX_TEXT_BYTES = 2 * 1024 * 1024
DOWNLOAD_TIMEOUT_SECONDS = 30
PARSER_TIMEOUT_SECONDS = 20
USER_AGENT = "Securus-Public-NBA-Attestor/1.0"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
PDF_PATH_RE = re.compile(
    r"^/referee/injury/Injury-Report_(\d{4}-\d{2}-\d{2})_"
    r"(\d{2})(?:_(\d{2}))?(AM|PM)\.pdf$"
)
REPORT_HEADER_RE = re.compile(
    r"^\s*Injury\s+Report:\s*(\d{2}/\d{2}/\d{2})\s+"
    r"(\d{1,2}:\d{2})\s*(AM|PM)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
PAGE_HEADER_RE = re.compile(
    r"^\s*Page\s+(\d+)\s+of\s+(\d+)\s*$", re.IGNORECASE | re.MULTILINE
)
SECTION_DATE_RE = re.compile(
    r"^\s*(\d{2}/\d{2}/\d{4})\s+\d{1,2}:\d{2}\s+\(ET\)\s+"
    r"[A-Z]{2,3}@[A-Z]{2,3}\b"
)
MATCHUP_RE = re.compile(r"\b[A-Z]{2,3}@[A-Z]{2,3}\b")
NBA_TEAMS = {
    "ATL": "Atlanta Hawks",
    "BKN": "Brooklyn Nets",
    "BOS": "Boston Celtics",
    "CHA": "Charlotte Hornets",
    "CHI": "Chicago Bulls",
    "CLE": "Cleveland Cavaliers",
    "DAL": "Dallas Mavericks",
    "DEN": "Denver Nuggets",
    "DET": "Detroit Pistons",
    "GSW": "Golden State Warriors",
    "HOU": "Houston Rockets",
    "IND": "Indiana Pacers",
    "LAC": "LA Clippers",
    "LAL": "Los Angeles Lakers",
    "MEM": "Memphis Grizzlies",
    "MIA": "Miami Heat",
    "MIL": "Milwaukee Bucks",
    "MIN": "Minnesota Timberwolves",
    "NOP": "New Orleans Pelicans",
    "NYK": "New York Knicks",
    "OKC": "Oklahoma City Thunder",
    "ORL": "Orlando Magic",
    "PHI": "Philadelphia 76ers",
    "PHX": "Phoenix Suns",
    "POR": "Portland Trail Blazers",
    "SAC": "Sacramento Kings",
    "SAS": "San Antonio Spurs",
    "TOR": "Toronto Raptors",
    "UTA": "Utah Jazz",
    "WAS": "Washington Wizards",
}
NOT_SUBMITTED_RE = re.compile(r"\bNOT\s+YET\s+SUBMITTED\b", re.IGNORECASE)
TABLE_HEADER_RE = re.compile(
    r"\bGame\s+Date\s+Game\s+Time\s+Matchup\s+Team\s+Player\s+Name\s+"
    r"Current\s+Status\s+Reason\b",
    re.IGNORECASE,
)


class AttestationError(RuntimeError):
    """An attestation input or transport failed closed."""


class NoRedirectHandler(HTTPRedirectHandler):
    """Reject redirects so credentials and evidence never change origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        raise AttestationError(f"redirect refused (HTTP {code})")


def _canonical_json(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def encode_payload(payload: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(_canonical_json(payload)).decode("ascii").rstrip("=")


def decode_payload(value: str, label: str) -> dict[str, Any]:
    if not value or len(value) > MAX_JSON_BYTES * 2:
        raise AttestationError(f"{label} output is missing or oversized")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise AttestationError(f"{label} output is not canonical base64url")
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        payload = json.loads(raw)
    except (ValueError, json.JSONDecodeError) as error:
        raise AttestationError(f"{label} output is invalid") from error
    if len(raw) > MAX_JSON_BYTES or not isinstance(payload, dict):
        raise AttestationError(f"{label} output is not a bounded JSON object")
    return payload


def _parse_iso_timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise AttestationError(f"{field} is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise AttestationError(f"{field} is invalid") from error
    if parsed.utcoffset() is None:
        raise AttestationError(f"{field} must include a timezone")
    return parsed


def _parse_date(value: object, field: str) -> date:
    if not isinstance(value, str):
        raise AttestationError(f"{field} is missing")
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise AttestationError(f"{field} is invalid") from error


def _validate_matchups(
    value: object,
    field: str,
    *,
    allow_empty: bool = False,
) -> list[str]:
    if not isinstance(value, list) or len(value) > 15:
        raise AttestationError(f"{field} must be a bounded matchup list")
    if not value and not allow_empty:
        raise AttestationError(f"{field} must contain at least one matchup")

    matchups: list[str] = []
    for entry in value:
        if not isinstance(entry, str) or not re.fullmatch(
            r"[A-Z]{2,3}@[A-Z]{2,3}", entry
        ):
            raise AttestationError(f"{field} contains a noncanonical matchup")
        away, home = entry.split("@", 1)
        if away not in NBA_TEAMS or home not in NBA_TEAMS or away == home:
            raise AttestationError(f"{field} contains an invalid NBA matchup")
        matchups.append(entry)

    if matchups != sorted(set(matchups)):
        raise AttestationError(f"{field} must be sorted and unique")
    return matchups


def validate_site_url(base_url: str) -> str:
    parts = urlsplit(base_url)
    try:
        port = parts.port
    except ValueError as error:
        raise AttestationError("Securus URL contains an invalid port") from error
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or parts.path not in ("", "/")
        or port not in (None, 443)
    ):
        raise AttestationError("Securus URL must be an HTTPS origin")
    return f"https://{parts.hostname}"


def validate_report_url(report_url: object, report_date: str) -> str:
    if not isinstance(report_url, str):
        raise AttestationError("target.reportUrl is missing")
    parts = urlsplit(report_url)
    try:
        port = parts.port
    except ValueError as error:
        raise AttestationError("target.reportUrl contains an invalid port") from error
    match = PDF_PATH_RE.fullmatch(parts.path)
    if (
        parts.scheme != "https"
        or parts.hostname != PDF_HOST
        or parts.username is not None
        or parts.password is not None
        or port not in (None, 443)
        or parts.query
        or parts.fragment
        or match is None
        or match.group(1) != report_date
        or not 1 <= int(match.group(2)) <= 12
        or not 0 <= int(match.group(3) or "30") <= 59
    ):
        raise AttestationError("target.reportUrl is not an allowlisted NBA report")
    return report_url


def validate_challenge(payload: dict[str, Any]) -> dict[str, Any]:
    required = payload.get("required")
    if not isinstance(required, bool):
        raise AttestationError("challenge.required must be boolean")
    if not required:
        return {"required": False}

    if payload.get("schemaVersion") != SCHEMA_VERSION:
        raise AttestationError(
            f"challenge.schemaVersion must be {SCHEMA_VERSION}"
        )

    verifier_version = payload.get("verifierVersion")
    if verifier_version != VERIFIER_VERSION:
        raise AttestationError("challenge.verifierVersion is invalid")
    target = payload.get("target")
    if not isinstance(target, dict):
        raise AttestationError("challenge.target is missing")

    report_date_text = target.get("reportDate")
    report_date = _parse_date(report_date_text, "target.reportDate")
    target_game_date = _parse_date(
        target.get("targetGameDate"), "target.targetGameDate"
    )
    if target_game_date < report_date or (target_game_date - report_date).days > 1:
        raise AttestationError("target.targetGameDate is outside the report window")
    report_published_at = target.get("reportPublishedAt")
    _parse_iso_timestamp(report_published_at, "target.reportPublishedAt")
    filename_published_at = target.get("filenamePublishedAt")
    if filename_published_at is not None:
        filename_time = _parse_iso_timestamp(
            filename_published_at, "target.filenamePublishedAt"
        )
        if filename_time.astimezone(ZoneInfo("America/New_York")).date() != report_date:
            raise AttestationError("target.filenamePublishedAt has the wrong report date")
    sha256 = target.get("sha256")
    if not isinstance(sha256, str) or not SHA256_RE.fullmatch(sha256):
        raise AttestationError("target.sha256 must be a lowercase SHA-256 digest")
    byte_count = target.get("bytes")
    if (
        isinstance(byte_count, bool)
        or not isinstance(byte_count, int)
        or not MIN_PDF_BYTES <= byte_count <= MAX_PDF_BYTES
    ):
        raise AttestationError("target.bytes is outside the allowed PDF size")
    report_url = validate_report_url(target.get("reportUrl"), report_date.isoformat())
    expected_matchups = _validate_matchups(
        target.get("expectedMatchups"), "target.expectedMatchups"
    )
    schedule_captured_at = target.get("scheduleCapturedAt")
    schedule_completed_at = target.get("scheduleCompletedAt")
    _parse_iso_timestamp(schedule_captured_at, "target.scheduleCapturedAt")
    _parse_iso_timestamp(schedule_completed_at, "target.scheduleCompletedAt")

    normalized_target = {
        "reportUrl": report_url,
        "reportDate": report_date.isoformat(),
        "reportPublishedAt": report_published_at,
        "sha256": sha256,
        "bytes": byte_count,
        "targetGameDate": target_game_date.isoformat(),
        "expectedMatchups": expected_matchups,
        "scheduleCapturedAt": schedule_captured_at,
        "scheduleCompletedAt": schedule_completed_at,
    }
    if filename_published_at is not None:
        normalized_target["filenamePublishedAt"] = filename_published_at

    return {
        "required": True,
        "schemaVersion": SCHEMA_VERSION,
        "verifierVersion": verifier_version,
        "target": normalized_target,
    }


def validate_attestation(payload: dict[str, Any]) -> dict[str, Any]:
    expected_keys = {
        "schemaVersion",
        "verifierVersion",
        "reportUrl",
        "reportDate",
        "reportPublishedAt",
        "sha256",
        "bytes",
        "targetGameDate",
        "expectedMatchups",
        "scheduleCapturedAt",
        "scheduleCompletedAt",
        "parserId",
        "parserVersion",
        "extractionSucceeded",
        "schemaAnchorsVerified",
        "targetDateSectionFound",
        "reportedMatchups",
        "teamBlocksComplete",
        "targetMatchupCount",
        "notYetSubmittedCount",
        "textBytes",
    }
    if set(payload) != expected_keys:
        raise AttestationError(
            f"attestation fields do not match schema version {SCHEMA_VERSION}"
        )
    if payload.get("schemaVersion") != SCHEMA_VERSION:
        raise AttestationError(
            f"attestation.schemaVersion must be {SCHEMA_VERSION}"
        )
    verifier_version = payload.get("verifierVersion")
    if verifier_version != VERIFIER_VERSION:
        raise AttestationError("attestation.verifierVersion is invalid")

    report_date = _parse_date(payload.get("reportDate"), "attestation.reportDate")
    target_date = _parse_date(
        payload.get("targetGameDate"), "attestation.targetGameDate"
    )
    if target_date < report_date or (target_date - report_date).days > 1:
        raise AttestationError("attestation target date is outside the report window")
    validate_report_url(payload.get("reportUrl"), report_date.isoformat())
    _parse_iso_timestamp(
        payload.get("reportPublishedAt"), "attestation.reportPublishedAt"
    )
    _parse_iso_timestamp(
        payload.get("scheduleCapturedAt"), "attestation.scheduleCapturedAt"
    )
    _parse_iso_timestamp(
        payload.get("scheduleCompletedAt"), "attestation.scheduleCompletedAt"
    )
    _validate_matchups(
        payload.get("expectedMatchups"), "attestation.expectedMatchups"
    )
    reported_matchups = _validate_matchups(
        payload.get("reportedMatchups"),
        "attestation.reportedMatchups",
        allow_empty=True,
    )
    if not isinstance(payload.get("sha256"), str) or not SHA256_RE.fullmatch(
        payload["sha256"]
    ):
        raise AttestationError("attestation.sha256 is invalid")
    byte_count = payload.get("bytes")
    if (
        isinstance(byte_count, bool)
        or not isinstance(byte_count, int)
        or not MIN_PDF_BYTES <= byte_count <= MAX_PDF_BYTES
    ):
        raise AttestationError("attestation.bytes is invalid")
    if payload.get("parserId") != PARSER_ID:
        raise AttestationError("attestation.parserId is invalid")
    parser_version = payload.get("parserVersion")
    if not isinstance(parser_version, str) or not re.fullmatch(
        r"[0-9][0-9A-Za-z.+~:-]{0,39}", parser_version
    ):
        raise AttestationError("attestation.parserVersion is invalid")

    for field in (
        "extractionSucceeded",
        "schemaAnchorsVerified",
        "targetDateSectionFound",
        "teamBlocksComplete",
    ):
        if not isinstance(payload.get(field), bool):
            raise AttestationError(f"attestation.{field} must be boolean")
    for field in ("targetMatchupCount", "notYetSubmittedCount", "textBytes"):
        value = payload.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise AttestationError(f"attestation.{field} must be a nonnegative integer")
    if payload["textBytes"] > MAX_TEXT_BYTES:
        raise AttestationError("attestation.textBytes exceeds the allowed size")
    if payload["targetMatchupCount"] > 15:
        raise AttestationError("attestation.targetMatchupCount exceeds the NBA slate limit")
    if payload["targetMatchupCount"] != len(reported_matchups):
        raise AttestationError(
            "attestation.targetMatchupCount must match reportedMatchups"
        )
    if payload["notYetSubmittedCount"] > 30:
        raise AttestationError("attestation.notYetSubmittedCount exceeds the team limit")
    if not payload["extractionSucceeded"] and any(
        (
            payload["schemaAnchorsVerified"],
            payload["targetDateSectionFound"],
            payload["teamBlocksComplete"],
            payload["targetMatchupCount"],
            bool(reported_matchups),
            payload["notYetSubmittedCount"],
            payload["textBytes"],
        )
    ):
        raise AttestationError("failed extraction must not claim parsed evidence")
    return payload


def _read_json_response(response: Any) -> dict[str, Any]:
    raw = response.read(MAX_JSON_BYTES + 1)
    if len(raw) > MAX_JSON_BYTES:
        raise AttestationError("Securus response exceeded the JSON size limit")
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AttestationError("Securus returned invalid JSON") from error
    if not isinstance(payload, dict):
        raise AttestationError("Securus returned a non-object JSON response")
    return payload


def site_request_json(
    base_url: str,
    token: str,
    *,
    method: str,
    payload: dict[str, Any] | None = None,
    opener: Any | None = None,
) -> dict[str, Any]:
    if not token:
        raise AttestationError("Securus OIDC identity is unavailable")
    if method not in ("GET", "POST"):
        raise AttestationError("unsupported Securus attestation method")
    origin = validate_site_url(base_url)
    body = _canonical_json(payload) if payload is not None else None
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "User-Agent": USER_AGENT,
    }
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = Request(
        f"{origin}{ATTESTATION_PATH}", data=body, method=method, headers=headers
    )
    client = opener or build_opener(NoRedirectHandler())
    try:
        with client.open(request, timeout=30) as response:
            expected_status = 200 if method == "GET" else 201
            if response.getcode() != expected_status:
                raise AttestationError(
                    f"Securus attestation endpoint returned HTTP {response.getcode()}"
                )
            return _read_json_response(response)
    except HTTPError as error:
        raise AttestationError(
            f"Securus attestation endpoint returned HTTP {error.code}"
        ) from error
    except URLError as error:
        raise AttestationError("Securus attestation endpoint is unavailable") from error


def fetch_challenge(
    base_url: str,
    token: str,
    requester: Callable[..., dict[str, Any]] = site_request_json,
) -> dict[str, Any]:
    return validate_challenge(requester(base_url, token, method="GET"))


def submit_attestation(
    base_url: str,
    token: str,
    attestation: dict[str, Any],
    requester: Callable[..., dict[str, Any]] = site_request_json,
) -> dict[str, Any]:
    bounded = validate_attestation(attestation)
    response = requester(base_url, token, method="POST", payload=bounded)
    expected_complete = bool(
        bounded["extractionSucceeded"]
        and bounded["schemaAnchorsVerified"]
        and bounded["targetDateSectionFound"]
        and bounded["teamBlocksComplete"]
        and bounded["reportedMatchups"] == bounded["expectedMatchups"]
        and bounded["targetMatchupCount"] == len(bounded["expectedMatchups"])
        and bounded["notYetSubmittedCount"] == 0
        and bounded["textBytes"] >= 100
    )
    if (
        response.get("required") is not True
        or response.get("stored") is not True
        or response.get("complete") is not expected_complete
        or response.get("sha256") != bounded["sha256"]
        or response.get("verifierVersion") != bounded["verifierVersion"]
    ):
        raise AttestationError("Securus did not confirm the exact NBA attestation")
    return response


def assert_credential_free_environment(environ: dict[str, str] | None = None) -> None:
    environment = environ if environ is not None else os.environ
    forbidden = (
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
        "ACTIONS_ID_TOKEN_REQUEST_URL",
        "SECURUS_OIDC_TOKEN",
    )
    present = [name for name in forbidden if environment.get(name)]
    if present:
        raise AttestationError(
            "PDF verification job unexpectedly has an OIDC credential capability"
        )


def _content_type(headers: Any) -> str:
    if hasattr(headers, "get_content_type"):
        return str(headers.get_content_type()).lower()
    return str(headers.get("Content-Type", "")).split(";", 1)[0].strip().lower()


def download_exact_pdf(
    target: dict[str, Any],
    destination: Path,
    *,
    opener: Any | None = None,
) -> None:
    report_url = validate_report_url(target.get("reportUrl"), target["reportDate"])
    expected_bytes = target["bytes"]
    expected_digest = target["sha256"]
    request = Request(
        report_url,
        headers={
            "Accept": "application/pdf",
            "Accept-Encoding": "identity",
            "User-Agent": USER_AGENT,
        },
    )
    client = opener or build_opener(NoRedirectHandler())
    digest = hashlib.sha256()
    total = 0
    first = b""
    tail = b""
    try:
        with client.open(request, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
            if response.getcode() != 200:
                raise AttestationError(
                    f"NBA report returned HTTP {response.getcode()}"
                )
            effective_url = response.geturl() if hasattr(response, "geturl") else report_url
            if effective_url != report_url:
                raise AttestationError("NBA report URL changed during download")
            if _content_type(response.headers) != "application/pdf":
                raise AttestationError("NBA report did not return application/pdf")
            if str(response.headers.get("Content-Encoding", "")).lower() not in (
                "",
                "identity",
            ):
                raise AttestationError("NBA report used an unexpected content encoding")
            content_length = response.headers.get("Content-Length")
            if content_length:
                try:
                    declared_length = int(content_length)
                except ValueError as error:
                    raise AttestationError(
                        "NBA report Content-Length is invalid"
                    ) from error
                if declared_length != expected_bytes:
                    raise AttestationError(
                        "NBA report Content-Length does not match the challenge"
                    )

            with destination.open("xb") as output:
                while True:
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_PDF_BYTES or total > expected_bytes:
                        raise AttestationError("NBA report exceeded the expected size")
                    if not first:
                        first = chunk[:8]
                    tail = (tail + chunk)[-1_024:]
                    digest.update(chunk)
                    output.write(chunk)
    except HTTPError as error:
        raise AttestationError(f"NBA report returned HTTP {error.code}") from error
    except URLError as error:
        raise AttestationError("NBA report download failed") from error

    if total != expected_bytes:
        raise AttestationError("NBA report byte count does not match the challenge")
    if not hmac.compare_digest(digest.hexdigest(), expected_digest):
        raise AttestationError("NBA report SHA-256 does not match the challenge")
    if not first.startswith(b"%PDF-") or b"%%EOF" not in tail:
        raise AttestationError("NBA report PDF envelope is invalid")
    destination.chmod(0o600)


def pdftotext_version(
    runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
) -> str:
    try:
        result = runner(
            ["pdftotext", "-v"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as error:
        raise AttestationError("pdftotext is unavailable") from error
    combined = (result.stdout or b"") + b"\n" + (result.stderr or b"")
    match = re.search(rb"pdftotext version ([0-9][0-9A-Za-z.+~:-]{0,39})", combined)
    if result.returncode != 0 or match is None:
        raise AttestationError("pdftotext version could not be verified")
    return match.group(1).decode("ascii")


def extract_pdf_text(
    pdf_path: Path,
    text_path: Path,
    workdir: Path,
    runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
) -> tuple[str, int]:
    clean_environment = {
        "HOME": str(workdir),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/bin:/bin",
    }
    try:
        result = runner(
            [
                "pdftotext",
                "-layout",
                "-enc",
                "UTF-8",
                "-nopgbrk",
                str(pdf_path),
                str(text_path),
            ],
            cwd=workdir,
            env=clean_environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=PARSER_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError as error:
        raise AttestationError("pdftotext disappeared before extraction") from error
    except subprocess.TimeoutExpired as error:
        raise AttestationError("pdftotext timed out") from error
    if result.returncode != 0 or not text_path.is_file():
        raise AttestationError("pdftotext could not extract the verified PDF")
    size = text_path.stat().st_size
    if size < 1 or size > MAX_TEXT_BYTES:
        raise AttestationError("pdftotext output was empty or oversized")
    try:
        text = text_path.read_text(encoding="utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise AttestationError("pdftotext output was not valid UTF-8") from error
    if not text.strip() or "\x00" in text or "\ufffd" in text:
        raise AttestationError("pdftotext output did not contain valid report text")
    return text, size


def analyze_extracted_text(
    text: str,
    target_game_date: str,
    header_published_at: str,
) -> dict[str, Any]:
    normalized = unicodedata.normalize("NFKC", text)
    published = _parse_iso_timestamp(header_published_at, "target report timestamp")
    published_et = published.astimezone(ZoneInfo("America/New_York"))
    expected_header = (
        published_et.strftime("%m/%d/%y"),
        published_et.strftime("%I:%M").lstrip("0"),
        published_et.strftime("%p"),
    )
    headers = [
        (match.group(1), match.group(2).lstrip("0"), match.group(3).upper())
        for match in REPORT_HEADER_RE.finditer(normalized)
    ]
    header_valid = bool(headers) and all(header == expected_header for header in headers)
    page_headers = [
        (int(match.group(1)), int(match.group(2)))
        for match in PAGE_HEADER_RE.finditer(normalized)
    ]
    page_total = page_headers[0][1] if page_headers else 0
    pages_valid = bool(page_headers) and all(
        total == page_total for _, total in page_headers
    ) and {page for page, _ in page_headers} == set(range(1, page_total + 1))
    flattened = re.sub(r"\s+", " ", normalized)
    table_header_valid = TABLE_HEADER_RE.search(flattened) is not None

    sections: dict[str, list[str]] = {}
    current_date: str | None = None
    for line in normalized.splitlines():
        match = SECTION_DATE_RE.match(line)
        if match:
            try:
                current_date = datetime.strptime(match.group(1), "%m/%d/%Y").date().isoformat()
            except ValueError:
                current_date = None
        if current_date is not None:
            sections.setdefault(current_date, []).append(line)

    target_lines = sections.get(target_game_date, [])
    target_text = re.sub(r"\s+", " ", "\n".join(target_lines))
    target_upper = target_text.upper()
    matchup_matches = list(MATCHUP_RE.finditer(target_upper))
    matchup_rows = [match.group(0) for match in matchup_matches]
    reported_matchups: list[str] = []
    matchup_codes_valid = len(matchup_rows) == len(set(matchup_rows))
    team_blocks_complete = bool(matchup_rows)
    for index, match in enumerate(matchup_matches):
        away, home = match.group(0).split("@", 1)
        block_end = (
            matchup_matches[index + 1].start()
            if index + 1 < len(matchup_matches)
            else len(target_upper)
        )
        block = target_upper[match.end():block_end]
        matchup_valid = (
            away in NBA_TEAMS and home in NBA_TEAMS and away != home
        )
        if matchup_valid:
            reported_matchups.append(match.group(0))
        else:
            matchup_codes_valid = False
        if (
            not matchup_valid
            or NBA_TEAMS.get(away, "").upper() not in block
            or NBA_TEAMS.get(home, "").upper() not in block
        ):
            team_blocks_complete = False
    reported_matchups = sorted(set(reported_matchups))
    not_submitted = len(NOT_SUBMITTED_RE.findall(target_text))
    return {
        "schemaAnchorsVerified": (
            header_valid and pages_valid and table_header_valid and matchup_codes_valid
        ),
        "targetDateSectionFound": bool(target_lines),
        "reportedMatchups": reported_matchups,
        "teamBlocksComplete": team_blocks_complete,
        "targetMatchupCount": len(reported_matchups),
        "notYetSubmittedCount": not_submitted,
    }


def verify_challenge(
    challenge: dict[str, Any],
    *,
    pdf_opener: Any | None = None,
    runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
) -> dict[str, Any] | None:
    bounded = validate_challenge(challenge)
    if not bounded["required"]:
        return None
    target = bounded["target"]
    parser_version = pdftotext_version(runner)
    with tempfile.TemporaryDirectory(prefix="securus-nba-") as temporary:
        workdir = Path(temporary)
        workdir.chmod(0o700)
        pdf_path = workdir / "report.pdf"
        text_path = workdir / "report.txt"
        download_exact_pdf(target, pdf_path, opener=pdf_opener)
        text, text_bytes = extract_pdf_text(pdf_path, text_path, workdir, runner)
        findings = analyze_extracted_text(
            text,
            target["targetGameDate"],
            target.get("filenamePublishedAt", target["reportPublishedAt"]),
        )

    attestation = {
        "schemaVersion": SCHEMA_VERSION,
        "verifierVersion": bounded["verifierVersion"],
        "reportUrl": target["reportUrl"],
        "reportDate": target["reportDate"],
        "reportPublishedAt": target["reportPublishedAt"],
        "sha256": target["sha256"],
        "bytes": target["bytes"],
        "targetGameDate": target["targetGameDate"],
        "expectedMatchups": target["expectedMatchups"],
        "scheduleCapturedAt": target["scheduleCapturedAt"],
        "scheduleCompletedAt": target["scheduleCompletedAt"],
        "parserId": PARSER_ID,
        "parserVersion": parser_version,
        "extractionSucceeded": True,
        "schemaAnchorsVerified": findings["schemaAnchorsVerified"],
        "targetDateSectionFound": findings["targetDateSectionFound"],
        "reportedMatchups": findings["reportedMatchups"],
        "teamBlocksComplete": findings["teamBlocksComplete"],
        "targetMatchupCount": findings["targetMatchupCount"],
        "notYetSubmittedCount": findings["notYetSubmittedCount"],
        "textBytes": text_bytes,
    }
    return validate_attestation(attestation)


def write_github_output(path: str, key: str, value: str) -> None:
    if not path:
        raise AttestationError("GITHUB_OUTPUT path is unavailable")
    if not re.fullmatch(r"[a-z_]+", key) or "\n" in value or "\r" in value:
        raise AttestationError("refusing an unsafe workflow output")
    with Path(path).open("a", encoding="utf-8") as output:
        output.write(f"{key}={value}\n")


def _challenge_command(args: argparse.Namespace) -> int:
    challenge = fetch_challenge(
        args.url, os.environ.get("SECURUS_OIDC_TOKEN", "")
    )
    write_github_output(args.github_output, "required", str(challenge["required"]).lower())
    write_github_output(args.github_output, "challenge", encode_payload(challenge))
    print(
        "NBA injury-report verification "
        + ("is required." if challenge["required"] else "is not required.")
    )
    return 0


def _verify_command(args: argparse.Namespace) -> int:
    assert_credential_free_environment()
    challenge = decode_payload(args.challenge, "challenge")
    attestation = verify_challenge(challenge)
    if attestation is None:
        print("NBA injury-report verification is not required.")
        return 0
    write_github_output(args.github_output, "attestation", encode_payload(attestation))
    print(
        "NBA PDF evidence verified and bounded "
        f"({attestation['targetMatchupCount']} target matchups, "
        f"{attestation['notYetSubmittedCount']} not yet submitted)."
    )
    return 0


def _submit_command(args: argparse.Namespace) -> int:
    attestation = decode_payload(args.attestation, "attestation")
    submit_attestation(
        args.url,
        os.environ.get("SECURUS_OIDC_TOKEN", ""),
        attestation,
    )
    print("Securus accepted the hash-bound NBA availability evidence.")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)

    challenge = commands.add_parser("challenge")
    challenge.add_argument("url")
    challenge.add_argument("--github-output", required=True)
    challenge.set_defaults(handler=_challenge_command)

    verify = commands.add_parser("verify")
    verify.add_argument(
        "--challenge",
        default=os.environ.get("NBA_ATTESTATION_CHALLENGE", ""),
    )
    verify.add_argument("--github-output", required=True)
    verify.set_defaults(handler=_verify_command)

    submit = commands.add_parser("submit")
    submit.add_argument("url")
    submit.add_argument(
        "--attestation",
        default=os.environ.get("NBA_ATTESTATION_RESULT", ""),
    )
    submit.set_defaults(handler=_submit_command)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return args.handler(args)
    except AttestationError as error:
        print(f"NBA injury-report attestation failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

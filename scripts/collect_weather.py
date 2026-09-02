#!/usr/bin/env python3
"""Collect bounded event-time MLB/NFL forecasts from keyless Open-Meteo."""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


SOURCE_ID = "open-meteo"
USER_AGENT = "Securus-GitHub-Collector/1.0"
ACTION_BOOKS = "68,69,71,75,79,123,247,972"
HOURLY_FIELDS = (
    "temperature_2m,apparent_temperature,precipitation_probability,"
    "precipitation,weather_code,wind_speed_10m,wind_gusts_10m,wind_direction_10m"
)

MLB_VENUES: dict[str, tuple[float, float, bool]] = {
    "ARI": (33.4453, -112.0667, True), "ATL": (33.8907, -84.4677, False),
    "ATH": (38.5800, -121.5133, False), "OAK": (38.5800, -121.5133, False),
    "BAL": (39.2839, -76.6217, False), "BOS": (42.3467, -71.0972, False),
    "CHC": (41.9484, -87.6553, False), "CWS": (41.8300, -87.6338, False),
    "CIN": (39.0979, -84.5082, False), "CLE": (41.4962, -81.6852, False),
    "COL": (39.7559, -104.9942, False), "DET": (42.3390, -83.0485, False),
    "HOU": (29.7573, -95.3555, True), "KC": (39.0517, -94.4803, False),
    "LAA": (33.8003, -117.8827, False), "LAD": (34.0739, -118.2400, False),
    "MIA": (25.7781, -80.2197, True), "MIL": (43.0280, -87.9712, True),
    "MIN": (44.9817, -93.2776, False), "NYM": (40.7571, -73.8458, False),
    "NYY": (40.8296, -73.9262, False), "PHI": (39.9061, -75.1665, False),
    "PIT": (40.4469, -80.0057, False), "SD": (32.7076, -117.1570, False),
    "SEA": (47.5914, -122.3325, True), "SF": (37.7786, -122.3893, False),
    "STL": (38.6226, -90.1928, False), "TB": (27.9803, -82.5068, True),
    "TEX": (32.7473, -97.0847, True), "TOR": (43.6414, -79.3894, True),
    "WAS": (38.8730, -77.0074, False), "WSH": (38.8730, -77.0074, False),
}

NFL_VENUES: dict[str, tuple[float, float, bool]] = {
    "ARI": (33.5276, -112.2626, True), "ATL": (33.7554, -84.4008, True),
    "BAL": (39.2780, -76.6227, False), "BUF": (42.7738, -78.7870, False),
    "CAR": (35.2258, -80.8528, False), "CHI": (41.8623, -87.6167, False),
    "CIN": (39.0954, -84.5160, False), "CLE": (41.5061, -81.6995, False),
    "DAL": (32.7473, -97.0945, True), "DEN": (39.7439, -105.0201, False),
    "DET": (42.3400, -83.0456, True), "GB": (44.5013, -88.0622, False),
    "HOU": (29.6847, -95.4107, True), "IND": (39.7601, -86.1639, True),
    "JAC": (30.3239, -81.6373, False), "JAX": (30.3239, -81.6373, False),
    "KC": (39.0489, -94.4839, False), "LA": (33.9535, -118.3392, True),
    "LAR": (33.9535, -118.3392, True), "LAC": (33.9535, -118.3392, True),
    "LV": (36.0908, -115.1830, True), "MIA": (25.9580, -80.2389, False),
    "MIN": (44.9738, -93.2581, True), "NE": (42.0909, -71.2643, False),
    "NO": (29.9511, -90.0812, True), "NYG": (40.8135, -74.0745, False),
    "NYJ": (40.8135, -74.0745, False), "PHI": (39.9008, -75.1675, False),
    "PIT": (40.4468, -80.0158, False), "SEA": (47.5952, -122.3316, False),
    "SF": (37.4033, -121.9694, False), "TB": (27.9759, -82.5033, False),
    "TEN": (36.1665, -86.7713, False), "WAS": (38.9077, -76.8645, False),
    "WSH": (38.9077, -76.8645, False),
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def minute_timestamp() -> str:
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    return now.isoformat().replace("+00:00", "Z")


def fetch_json(url: str, attempts: int = 5) -> Any:
    last_error: Exception | None = None
    for attempt in range(attempts):
        request = Request(
            url,
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
        )
        try:
            with urlopen(request, timeout=45) as response:
                body = response.read()
        except HTTPError as error:
            last_error = error
            if error.code != 429 and error.code < 500:
                detail = error.read().decode("utf-8", errors="replace")[:300]
                raise RuntimeError(f"HTTP {error.code}: {detail}") from error
            retry_after = error.headers.get("Retry-After")
            delay = min(15.0, max(2.0, float(retry_after or 0), 2.0 ** attempt))
        except (URLError, TimeoutError) as error:
            last_error = error
            delay = min(15.0, 2.0 ** attempt)
        else:
            try:
                return json.loads(body)
            except ValueError:
                # An overloaded provider can answer 200 with an empty or HTML
                # body. Treat that like a transient failure instead of losing
                # the whole batch on the first malformed response.
                last_error = RuntimeError(f"malformed JSON response: {body[:80]!r}")
                delay = min(15.0, max(2.0, 2.0 ** attempt))
        if attempt + 1 < attempts:
            time.sleep(delay)
    raise RuntimeError(f"request failed after {attempts} attempts: {last_error}")


def post_json(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    securus_url = os.environ["SECURUS_URL"].rstrip("/")
    oidc_token = os.environ["SECURUS_OIDC_TOKEN"]
    request = Request(
        f"{securus_url}{path}",
        data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {oidc_token}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urlopen(request, timeout=45) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Securus returned HTTP {error.code}: {detail[:500]}") from error


def sync_run(status: str, records_written: int, started_at: str, error: str | None = None) -> None:
    post_json(
        "/api/data-ingest",
        {
            "kind": "sync-run",
            "records": [
                {
                    "sourceId": SOURCE_ID,
                    "status": status,
                    "recordsWritten": records_written,
                    "startedAt": started_at,
                    "completedAt": utc_now(),
                    "error": error,
                }
            ],
        },
    )


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def upcoming_events() -> list[dict[str, Any]]:
    central_now = datetime.now(ZoneInfo("America/Chicago"))
    lower = datetime.now(timezone.utc) - timedelta(hours=1)
    upper = datetime.now(timezone.utc) + timedelta(hours=48)
    events: dict[str, dict[str, Any]] = {}

    for day_offset in (0, 1, 2):
        date = (central_now + timedelta(days=day_offset)).strftime("%Y%m%d")
        for sport, venues in (("MLB", MLB_VENUES), ("NFL", NFL_VENUES)):
            league = sport.lower()
            query = urlencode(
                {
                    "period": "game",
                    "bookIds": ACTION_BOOKS,
                    "date": date,
                }
            )
            payload = fetch_json(f"https://api.actionnetwork.com/web/v1/scoreboard/{league}?{query}", attempts=3)
            for game in payload.get("games", []):
                event_id = str(game.get("id", ""))
                start_time = str(game.get("start_time", ""))
                if not event_id or not start_time:
                    continue
                try:
                    start = parse_time(start_time)
                except ValueError:
                    continue
                if start < lower or start > upper:
                    continue
                home_id = str(game.get("home_team_id", ""))
                home = next(
                    (team for team in game.get("teams", []) if str(team.get("id", "")) == home_id),
                    {},
                )
                abbreviation = str(home.get("abbr", "")).upper()
                coordinates = venues.get(abbreviation)
                if not coordinates:
                    continue
                event_key = f"action-network:{league}:{event_id}"
                events[event_key] = {
                    "sport": sport,
                    "eventKey": event_key,
                    "startTime": start_time,
                    "homeTeam": abbreviation,
                    "latitude": coordinates[0],
                    "longitude": coordinates[1],
                    "roof": coordinates[2],
                }
    return sorted(events.values(), key=lambda event: event["startTime"])[:32]


def event_records(events: list[dict[str, Any]], captured_at: str) -> tuple[list[dict[str, Any]], list[str]]:
    records: list[dict[str, Any]] = []
    warnings: list[str] = []
    for offset in range(0, len(events), 8):
        batch = events[offset : offset + 8]
        query = urlencode(
            {
                "latitude": ",".join(str(event["latitude"]) for event in batch),
                "longitude": ",".join(str(event["longitude"]) for event in batch),
                "hourly": HOURLY_FIELDS,
                "forecast_days": 3,
                "timezone": "UTC",
                "wind_speed_unit": "mph",
                "temperature_unit": "fahrenheit",
                "precipitation_unit": "inch",
            },
            safe=",",
        )
        try:
            response = fetch_json(f"https://api.open-meteo.com/v1/forecast?{query}")
            forecasts = response if isinstance(response, list) else [response]
            for index, event in enumerate(batch):
                forecast = forecasts[index] if index < len(forecasts) else {}
                hourly = forecast.get("hourly", {})
                times = hourly.get("time", [])
                if not times:
                    warnings.append(f"{event['eventKey']}: no hourly forecast")
                    continue
                target = parse_time(event["startTime"])
                nearest = min(
                    range(len(times)),
                    key=lambda position: abs(
                        datetime.fromisoformat(str(times[position])).replace(tzinfo=timezone.utc) - target
                    ),
                )

                def at(field: str) -> Any:
                    values = hourly.get(field, [])
                    return values[nearest] if nearest < len(values) else None

                forecast_time = f"{times[nearest]}Z"
                payload = {
                    "eventStartTime": event["startTime"],
                    "forecastTime": forecast_time,
                    "homeTeam": event["homeTeam"],
                    "latitude": event["latitude"],
                    "longitude": event["longitude"],
                    "roof": event["roof"],
                    "temperatureF": at("temperature_2m"),
                    "apparentTemperatureF": at("apparent_temperature"),
                    "precipitationProbability": at("precipitation_probability"),
                    "precipitationInches": at("precipitation"),
                    "weatherCode": at("weather_code"),
                    "windSpeedMph": at("wind_speed_10m"),
                    "windGustMph": at("wind_gusts_10m"),
                    "windDirectionDegrees": at("wind_direction_10m"),
                    "attribution": "Weather data by Open-Meteo.com (CC BY 4.0)",
                }
                records.append(
                    {
                        "source": SOURCE_ID,
                        "sport": event["sport"],
                        "recordType": "event-weather-forecast",
                        "recordKey": f"weather:{event['eventKey']}",
                        "eventKey": event["eventKey"],
                        "payload": payload,
                        "observedAt": forecast_time,
                        "capturedAt": captured_at,
                        "dedupeKey": json.dumps(payload, sort_keys=True, separators=(",", ":")),
                    }
                )
        except Exception as error:  # Keep independent batches fail-soft.
            warnings.append(f"forecast batch {offset // 8 + 1}: {str(error)[:240]}")
        if offset + 8 < len(events):
            time.sleep(1.25)
    return records, warnings


def main() -> int:
    started_at = utc_now()
    try:
        events = upcoming_events()
        if not events:
            warning = "no MLB or NFL events in the next 48 hours"
            sync_run("SUCCEEDED", 0, started_at, warning)
            print(json.dumps({"source": SOURCE_ID, "status": "SUCCEEDED", "records": 0, "warning": warning}))
            return 0
        records, warnings = event_records(events, minute_timestamp())
        if not records:
            raise RuntimeError("; ".join(warnings) or "no event forecasts returned")
        for index in range(0, len(records), 200):
            post_json("/api/data-ingest", {"kind": "stat", "records": records[index : index + 200]})
        warning = "; ".join(warnings)[:500] or None
        sync_run("SUCCEEDED", len(records), started_at, warning)
        print(
            json.dumps(
                {
                    "source": SOURCE_ID,
                    "status": "SUCCEEDED",
                    "events": len(events),
                    "records": len(records),
                    "warnings": warnings,
                }
            )
        )
        return 0
    except Exception as error:
        message = str(error)[:500]
        try:
            sync_run("FAILED", 0, started_at, message)
        except Exception as sync_error:
            message = f"{message}; sync reporting failed: {sync_error}"
        print(message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())


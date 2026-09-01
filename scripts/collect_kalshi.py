#!/usr/bin/env python3
"""Collect exact official Kalshi sports asks and settlement metadata."""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


SOURCE_ID = "kalshi"
USER_AGENT = "Securus-Public-Scheduler/1.0"
API_BASE = "https://external-api.kalshi.com/trade-api/v2"
CLIMATE_API_BASE = "https://api.elections.kalshi.com/trade-api/v2"
SERIES: dict[str, list[dict[str, Any]]] = {
    "MLB": [
        {"ticker": "KXMLBGAME", "market_family": "moneyline", "required": True},
        {"ticker": "KXMLBSPREAD", "market_family": "spread"},
        {"ticker": "KXMLBTOTAL", "market_family": "game_total"},
        {"ticker": "KXMLBTEAMTOTAL", "market_family": "team_total"},
        {"ticker": "KXMLBKS", "market_family": "player_prop", "prop_metric": "strikeouts"},
        {"ticker": "KXMLBHR", "market_family": "player_prop", "prop_metric": "home runs"},
    ],
    "NFL": [
        # Regulation ties make team-winner contracts observation-only. The
        # half-point spread family is the executable NFL readiness anchor.
        {"ticker": "KXNFLGAME", "market_family": "moneyline"},
        {"ticker": "KXNFLSPREAD", "market_family": "spread", "required": True},
        {"ticker": "KXNFLTOTAL", "market_family": "game_total"},
        {"ticker": "KXNFLTEAMTOTAL", "market_family": "team_total"},
    ],
    "NBA": [
        {"ticker": "KXNBAGAME", "market_family": "moneyline", "required": True},
        {"ticker": "KXNBASPREAD", "market_family": "spread"},
        {"ticker": "KXNBATOTAL", "market_family": "game_total"},
        {"ticker": "KXNBATEAMTOTAL", "market_family": "team_total"},
        {"ticker": "KXNBAPTS", "market_family": "player_prop", "prop_metric": "points"},
        {"ticker": "KXNBAREB", "market_family": "player_prop", "prop_metric": "rebounds"},
        {"ticker": "KXNBAAST", "market_family": "player_prop", "prop_metric": "assists"},
    ],
}

CLIMATE_DIRECTORY_SERIES = (
    "KXHIGHNY", "KXHIGHCHI", "KXHIGHLAX", "KXHIGHMIA", "KXHIGHTPHX",
    "KXHIGHTSFO", "KXHIGHTDAL", "KXHIGHTSEA", "KXHIGHTDC", "KXHIGHTSATX",
    "KXHIGHTNOLA", "KXHIGHAUS", "KXHIGHPHIL", "KXHIGHTATL", "KXHIGHTBOS",
    "KXHIGHTHOU", "KXHIGHDEN", "KXHIGHTLV",
    "KXLOWTNYC", "KXLOWTCHI", "KXLOWTPHX", "KXLOWTOKC", "KXLOWTSFO",
    "KXLOWTDAL", "KXLOWTSEA", "KXLOWTLAX", "KXLOWTMIN", "KXLOWTDEN",
    "KXLOWTATL", "KXLOWTAUS", "KXLOWTDC", "KXLOWTMIA", "KXLOWTHOU",
    "KXLOWTBOS", "KXLOWTPHIL", "KXLOWTSATX", "KXLOWTNOLA",
    "KXRAIN", "KXRAINWKND", "KXRAINCHIM", "KXRAINSEAM",
)
CLIMATE_BATCH_COUNT = 4
CLIMATE_MODELS = (
    ("gfs_seamless", "NOAA GFS"),
    ("ecmwf_ifs025", "ECMWF IFS"),
    ("icon_seamless", "DWD ICON"),
    ("gem_seamless", "ECCC GEM"),
    ("best_match", "Open-Meteo best match"),
)
CLIMATE_FALLBACK_STATIONS = {
    "KXHIGHNY": "KNYC", "KXLOWTNYC": "KNYC",
    "KXHIGHCHI": "KORD", "KXLOWTCHI": "KORD", "KXRAINCHIM": "KORD",
    "KXHIGHLAX": "KLAX", "KXLOWTLAX": "KLAX",
    "KXHIGHMIA": "KMIA", "KXLOWTMIA": "KMIA",
    "KXHIGHTPHX": "KPHX", "KXLOWTPHX": "KPHX",
    "KXHIGHTSFO": "KSFO", "KXLOWTSFO": "KSFO",
    "KXHIGHTDAL": "KDFW", "KXLOWTDAL": "KDFW",
    "KXHIGHTSEA": "KSEA", "KXLOWTSEA": "KSEA", "KXRAINSEAM": "KSEA",
    "KXHIGHTDC": "KDCA", "KXLOWTDC": "KDCA",
    "KXHIGHTSATX": "KSAT", "KXLOWTSATX": "KSAT",
    "KXHIGHTNOLA": "KMSY", "KXLOWTNOLA": "KMSY",
    "KXHIGHAUS": "KAUS", "KXLOWTAUS": "KAUS",
    "KXHIGHPHIL": "KPHL", "KXLOWTPHIL": "KPHL",
    "KXHIGHTATL": "KATL", "KXLOWTATL": "KATL",
    "KXHIGHTBOS": "KBOS", "KXLOWTBOS": "KBOS",
    "KXHIGHTHOU": "KIAH", "KXLOWTHOU": "KIAH",
    "KXHIGHDEN": "KDEN", "KXLOWTDEN": "KDEN",
    "KXHIGHTLV": "KLAS", "KXLOWTOKC": "KOKC", "KXLOWTMIN": "KMSP",
}

OPEN_PAST_GRACE = timedelta(hours=2)
OPEN_FUTURE_WINDOW = timedelta(hours=72)
SETTLED_HISTORY_WINDOW = timedelta(days=7)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def minute_timestamp() -> str:
    return datetime.now(timezone.utc).replace(second=0, microsecond=0).isoformat().replace("+00:00", "Z")


def fetch_json(url: str, attempts: int = 5) -> Any:
    last_error: Exception | None = None
    for attempt in range(attempts):
        request = Request(url, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
        try:
            with urlopen(request, timeout=45) as response:
                return json.load(response)
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
        if attempt + 1 < attempts:
            time.sleep(delay)
    raise RuntimeError(f"Request failed after {attempts} attempts: {last_error}")


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


def post_json(
    path: str,
    payload: dict[str, Any],
    timeout: int = 45,
) -> dict[str, Any]:
    request = Request(
        f"{os.environ['SECURUS_URL'].rstrip('/')}{path}",
        data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {securus_oidc_token()}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Securus returned HTTP {error.code}: {detail[:500]}") from error


def securus_json(path: str) -> dict[str, Any]:
    request = Request(
        f"{os.environ['SECURUS_URL'].rstrip('/')}{path}",
        headers={
            "Authorization": f"Bearer {securus_oidc_token()}",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urlopen(request, timeout=45) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Securus returned HTTP {error.code}: {detail[:500]}") from error


def post_batches(kind: str, records: list[dict[str, Any]]) -> None:
    for index in range(0, len(records), 400):
        post_json("/api/data-ingest", {"kind": kind, "records": records[index : index + 400]})


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


def as_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed and parsed not in (float("inf"), float("-inf")) else None


def parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def first_timestamp(*values: Any) -> datetime | None:
    for value in values:
        parsed = parse_timestamp(value)
        if parsed is not None:
            return parsed
    return None


def market_is_relevant(
    metadata: dict[str, Any],
    requested_status: str,
    reference_time: datetime,
) -> bool:
    if requested_status == "open":
        actionable_at = first_timestamp(
            metadata.get("occurrenceTime"),
            metadata.get("closeTime"),
            metadata.get("expectedExpirationTime"),
            metadata.get("expirationTime"),
            metadata.get("latestExpirationTime"),
        )
        return (
            actionable_at is not None
            and reference_time - OPEN_PAST_GRACE <= actionable_at
            <= reference_time + OPEN_FUTURE_WINDOW
        )

    settled_at = first_timestamp(
        metadata.get("updatedTime"),
        metadata.get("expirationTime"),
        metadata.get("latestExpirationTime"),
        metadata.get("occurrenceTime"),
    )
    return (
        settled_at is not None
        and reference_time - SETTLED_HISTORY_WINDOW <= settled_at
        <= reference_time + timedelta(days=1)
    )


def probability_to_american(value: Any) -> int | None:
    probability = as_float(value)
    if probability is None or not 0 < probability < 1:
        return None
    if probability >= 0.5:
        return -round(100 * probability / (1 - probability))
    return round(100 * (1 - probability) / probability)


def complementary_ask(bid: float | None) -> float | None:
    if bid is None or not 0 < bid < 1:
        return None
    return round(1 - bid, 4)


def market_metadata(
    market: dict[str, Any],
    event: dict[str, Any],
    series_config: dict[str, Any],
    requested_status: str,
    fee_type: str,
    fee_multiplier: float,
) -> dict[str, Any]:
    title = str(market.get("title") or market.get("yes_sub_title") or "Sports event")
    ticker = str(market.get("ticker") or "")
    market_key = f"{title} [{ticker}]" if ticker else title
    raw_status = str(market.get("status") or requested_status).strip().lower()
    status = "open" if requested_status == "open" and raw_status in {"active", "open"} else raw_status
    yes_bid_dollars = as_float(market.get("yes_bid_dollars"))
    no_bid_dollars = as_float(market.get("no_bid_dollars"))
    yes_bid_size = (
        as_float(market.get("yes_bid_size_fp"))
        if market.get("yes_bid_size_fp") is not None
        else as_float(market.get("yes_bid_size"))
    )
    no_bid_size = (
        as_float(market.get("no_bid_size_fp"))
        if market.get("no_bid_size_fp") is not None
        else as_float(market.get("no_bid_size"))
    )
    explicit_yes_ask_size = (
        as_float(market.get("yes_ask_size_fp"))
        if market.get("yes_ask_size_fp") is not None
        else as_float(market.get("yes_ask_size"))
    )
    explicit_no_ask_size = (
        as_float(market.get("no_ask_size_fp"))
        if market.get("no_ask_size_fp") is not None
        else as_float(market.get("no_ask_size"))
    )
    yes_ask_dollars = as_float(market.get("yes_ask_dollars"))
    no_ask_dollars = as_float(market.get("no_ask_dollars"))
    return {
        "ticker": ticker,
        "marketKey": market_key,
        "eventTicker": str(event.get("event_ticker") or market.get("event_ticker") or ""),
        "eventTitle": str(event.get("title") or ""),
        "eventSubTitle": str(event.get("sub_title") or ""),
        "title": title,
        "subtitle": str(market.get("subtitle") or ""),
        "yesSubTitle": str(market.get("yes_sub_title") or "yes"),
        "noSubTitle": str(market.get("no_sub_title") or "no"),
        "status": status,
        "result": str(market.get("result") or ""),
        "occurrenceTime": str(market.get("occurrence_datetime") or event.get("strike_date") or ""),
        "closeTime": str(market.get("close_time") or ""),
        "expectedExpirationTime": str(market.get("expected_expiration_time") or ""),
        "expirationTime": str(market.get("expiration_time") or ""),
        "latestExpirationTime": str(market.get("latest_expiration_time") or ""),
        "updatedTime": str(market.get("updated_time") or ""),
        "yesBidDollars": yes_bid_dollars,
        "noBidDollars": no_bid_dollars,
        "yesBidSize": yes_bid_size,
        "noBidSize": no_bid_size,
        "yesAskDollars": yes_ask_dollars
        if yes_ask_dollars is not None
        else complementary_ask(no_bid_dollars),
        "noAskDollars": no_ask_dollars
        if no_ask_dollars is not None
        else complementary_ask(yes_bid_dollars),
        "yesAskSize": explicit_yes_ask_size
        if explicit_yes_ask_size is not None
        else no_bid_size,
        "noAskSize": explicit_no_ask_size
        if explicit_no_ask_size is not None
        else yes_bid_size,
        "liquidityDollars": as_float(market.get("liquidity_dollars"))
        if market.get("liquidity_dollars") is not None
        else as_float(market.get("liquidity")),
        "volume": as_float(market.get("volume_fp"))
        if market.get("volume_fp") is not None
        else as_float(market.get("volume")),
        "functionalStrike": str(market.get("functional_strike") or ""),
        "floorStrike": as_float(market.get("floor_strike")),
        "capStrike": as_float(market.get("cap_strike")),
        "primaryParticipantKey": str(market.get("primary_participant_key") or ""),
        "mveCollectionTicker": str(market.get("mve_collection_ticker") or ""),
        "isProvisional": market.get("is_provisional") is True,
        "feeWaiverExpirationTime": str(market.get("fee_waiver_expiration_time") or ""),
        "feeType": fee_type,
        "feeMultiplier": fee_multiplier,
        "seriesTicker": str(series_config["ticker"]),
        "marketFamily": str(series_config["market_family"]),
        "propMetric": str(series_config.get("prop_metric") or ""),
    }


def collect_series(
    sport: str,
    series_config: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    ticker = str(series_config["ticker"])
    series_payload = fetch_json(f"{API_BASE}/series/{ticker}")
    series = series_payload.get("series") or {}
    fee_multiplier = as_float(series.get("fee_multiplier"))
    if fee_multiplier is None or fee_multiplier < 0:
        raise RuntimeError("Kalshi fee multiplier is unavailable")
    fee_type = str(series.get("fee_type") or "quadratic")

    captured_at = minute_timestamp()
    reference_time = parse_timestamp(captured_at) or datetime.now(timezone.utc)
    odds_records: list[dict[str, Any]] = []
    stat_records: list[dict[str, Any]] = []
    warnings: list[str] = []

    for requested_status in ("open",):
        event_limit = 200 if requested_status == "open" else 100
        query = urlencode(
            {
                "status": requested_status,
                "limit": event_limit,
                "with_nested_markets": "true",
                "series_ticker": ticker,
            }
        )
        payload = fetch_json(f"{API_BASE}/events?{query}")
        irrelevant_markets = 0
        for raw_event in payload.get("events", []):
            event = raw_event if isinstance(raw_event, dict) else {}
            for raw_market in event.get("markets", []):
                market = raw_market if isinstance(raw_market, dict) else {}
                metadata = market_metadata(
                    market,
                    event,
                    series_config,
                    requested_status,
                    fee_type,
                    fee_multiplier,
                )
                if not market_is_relevant(metadata, requested_status, reference_time):
                    irrelevant_markets += 1
                    continue
                event_key = f"kalshi:{metadata['eventTicker'] or metadata['ticker'] or 'unknown'}"
                stat_records.append(
                    {
                        "source": SOURCE_ID,
                        "sport": sport,
                        "recordType": "kalshi-market",
                        "recordKey": f"kalshi-market:{metadata['ticker'] or metadata['title']}",
                        "eventKey": event_key,
                        "payload": metadata,
                        "observedAt": metadata["updatedTime"] or captured_at,
                        "capturedAt": captured_at,
                        "dedupeKey": json.dumps(metadata, sort_keys=True, separators=(",", ":")),
                    }
                )

                if requested_status != "open":
                    continue
                outcomes = (
                    ("YES", metadata["yesAskDollars"]),
                    ("NO", metadata["noAskDollars"]),
                )
                line_value = metadata["floorStrike"]
                if line_value is None:
                    line_value = metadata["capStrike"]
                for selection, probability in outcomes:
                    american_odds = probability_to_american(probability)
                    if american_odds is None:
                        continue
                    odds_records.append(
                        {
                            "source": SOURCE_ID,
                            "sport": sport,
                            "eventKey": event_key,
                            "sportsbook": "Kalshi",
                            "market": metadata["marketKey"],
                            "selection": selection,
                            "lineValue": line_value,
                            "americanOdds": american_odds,
                            "capturedAt": captured_at,
                            # updated_time is non-trading metadata. This direct
                            # authoritative fetch observed the executable ask now.
                            "providerUpdatedAt": captured_at,
                        }
                    )

        if irrelevant_markets:
            warnings.append(
                f"discarded {irrelevant_markets} {requested_status} markets outside the actionable window"
            )
        if payload.get("cursor"):
            warnings.append(f"retained the first {event_limit} {requested_status} events")

    return odds_records, stat_records, warnings


def collect_sport(
    sport: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    odds_records: list[dict[str, Any]] = []
    stat_records: list[dict[str, Any]] = []
    warnings: list[str] = []
    required_series_failed = False
    successful_series = 0

    for series_config in SERIES[sport]:
        ticker = str(series_config["ticker"])
        try:
            series_odds, series_stats, series_warnings = collect_series(sport, series_config)
            odds_records.extend(series_odds)
            stat_records.extend(series_stats)
            warnings.extend(f"{sport}/{ticker}: {warning}" for warning in series_warnings)
            successful_series += 1
        except Exception as error:
            if series_config.get("required") is True:
                required_series_failed = True
            warnings.append(f"{sport}/{ticker}: {error}")

    if required_series_failed or successful_series == 0:
        raise RuntimeError("; ".join(warnings))
    return odds_records, stat_records, warnings


def climate_rotation(slot: int | None = None) -> tuple[int, list[str]]:
    current_slot = int(time.time() // 1800) if slot is None else slot
    batch_index = current_slot % CLIMATE_BATCH_COUNT
    return batch_index, [
        ticker
        for index, ticker in enumerate(CLIMATE_DIRECTORY_SERIES)
        if index % CLIMATE_BATCH_COUNT == batch_index
    ]


def fetch_climate_series(ticker: str) -> dict[str, Any]:
    series_payload = fetch_json(f"{API_BASE}/series/{ticker}")
    series = series_payload.get("series") or {}
    query = urlencode(
        {
            "status": "open",
            "limit": 200,
            "with_nested_markets": "true",
            "series_ticker": ticker,
        }
    )
    event_payload = fetch_json(f"{API_BASE}/events?{query}")
    markets: list[dict[str, Any]] = []
    for raw_event in event_payload.get("events", []):
        event = raw_event if isinstance(raw_event, dict) else {}
        for raw_market in event.get("markets", []):
            market = dict(raw_market) if isinstance(raw_market, dict) else {}
            market["event_ticker"] = str(
                market.get("event_ticker") or event.get("event_ticker") or ""
            )
            market["event_title"] = str(event.get("title") or "")
            markets.append(market)
    return {
        "ticker": ticker,
        "title": str(series.get("title") or ticker),
        "category": str(series.get("category") or "Climate and Weather"),
        "tags": series.get("tags") if isinstance(series.get("tags"), list) else [],
        "settlementSources": series.get("settlement_sources")
        if isinstance(series.get("settlement_sources"), list)
        else [],
        "feeType": str(series.get("fee_type") or "quadratic"),
        "feeMultiplier": as_float(series.get("fee_multiplier")),
        "productMetadata": series.get("product_metadata")
        if isinstance(series.get("product_metadata"), dict)
        else {},
        "markets": markets[:500],
    }


def climate_station_candidates(series: dict[str, Any]) -> list[str]:
    values = [str(series.get("title") or ""), str(series.get("ticker") or "")]
    for market in series.get("markets", [])[:80]:
        if not isinstance(market, dict):
            continue
        values.extend(
            str(market.get(field) or "")
            for field in (
                "event_title",
                "title",
                "subtitle",
                "rules_primary",
                "rules_secondary",
            )
        )
    text = " ".join(values).upper()
    codes: list[str] = []
    for match in re.finditer(r"\bCLI([A-Z0-9]{3,4})\b|\b([KP][A-Z0-9]{3})\b", text):
        code = str(match.group(1) or match.group(2) or "").upper()
        candidates = [code] if len(code) == 4 and code[:1] in {"K", "P"} else [f"K{code}", f"P{code}", code]
        for candidate in candidates:
            if re.fullmatch(r"[KP][A-Z0-9]{3}", candidate) and candidate not in codes:
                codes.append(candidate)
    fallback = CLIMATE_FALLBACK_STATIONS.get(str(series.get("ticker") or "").upper())
    if fallback and fallback not in codes:
        codes.append(fallback)
    return codes


def resolve_climate_station(
    series: dict[str, Any],
    station_cache: dict[str, dict[str, Any] | None],
) -> dict[str, Any] | None:
    for station_id in climate_station_candidates(series):
        if station_id not in station_cache:
            try:
                payload = fetch_json(
                    f"https://api.weather.gov/stations/{station_id}",
                    attempts=3,
                )
                properties = payload.get("properties") if isinstance(payload, dict) else {}
                geometry = payload.get("geometry") if isinstance(payload, dict) else {}
                coordinates = geometry.get("coordinates") if isinstance(geometry, dict) else []
                latitude = as_float(coordinates[1]) if isinstance(coordinates, list) and len(coordinates) > 1 else None
                longitude = as_float(coordinates[0]) if isinstance(coordinates, list) and coordinates else None
                if latitude is None or longitude is None:
                    station_cache[station_id] = None
                else:
                    station_cache[station_id] = {
                        "stationId": str(properties.get("stationIdentifier") or station_id).upper(),
                        "latitude": latitude,
                        "longitude": longitude,
                    }
            except Exception:
                station_cache[station_id] = None
        if station_cache[station_id]:
            return station_cache[station_id]
    return None


def collect_climate_forecasts(
    series_rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    failures: list[str] = []
    station_cache: dict[str, dict[str, Any] | None] = {}
    series_stations: list[tuple[str, dict[str, Any]]] = []
    for series in series_rows:
        ticker = str(series.get("ticker") or "").upper()
        station = resolve_climate_station(series, station_cache)
        if station:
            series_stations.append((ticker, station))
        else:
            failures.append(f"{ticker}: exact NWS station unavailable")

    unique_stations = {
        str(station["stationId"]): station
        for _, station in series_stations
    }
    models_by_station: dict[str, list[dict[str, Any]]] = {
        station_id: [] for station_id in unique_stations
    }
    ensembles_by_station: dict[str, dict[str, Any]] = {}
    stations = list(unique_stations.values())
    for model_id, label in CLIMATE_MODELS:
        for offset in range(0, len(stations), 10):
            batch = stations[offset : offset + 10]
            query = urlencode(
                {
                    "latitude": ",".join(str(station["latitude"]) for station in batch),
                    "longitude": ",".join(str(station["longitude"]) for station in batch),
                    "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,snowfall_sum",
                    "temperature_unit": "fahrenheit",
                    "precipitation_unit": "inch",
                    "timezone": "auto",
                    "forecast_days": 16,
                    "models": model_id,
                },
                safe=",",
            )
            try:
                payload = fetch_json(f"https://api.open-meteo.com/v1/forecast?{query}")
                responses = payload if isinstance(payload, list) else [payload]
                for index, station in enumerate(batch):
                    response = responses[index] if index < len(responses) and isinstance(responses[index], dict) else {}
                    daily = response.get("daily") if isinstance(response.get("daily"), dict) else {}
                    times = daily.get("time") if isinstance(daily.get("time"), list) else []
                    if not times:
                        failures.append(f"{station['stationId']}/{label}: no daily forecast")
                        continue
                    models_by_station[str(station["stationId"])].append(
                        {
                            "id": model_id,
                            "label": label,
                            "daily": {
                                field: list(daily.get(field, []))[:16]
                                if isinstance(daily.get(field), list)
                                else []
                                for field in (
                                    "time",
                                    "temperature_2m_max",
                                    "temperature_2m_min",
                                    "precipitation_sum",
                                    "snowfall_sum",
                                )
                            },
                        }
                    )
            except Exception as error:
                failures.append(f"{label} batch {offset // 10 + 1}: {str(error)[:180]}")
            if offset + 10 < len(stations):
                time.sleep(0.75)
        time.sleep(0.75)

    # Fetch true NOAA GEFS and ECMWF ensemble members in the public scheduler,
    # then relay compact station/date histograms. The Site does not burst the
    # provider directly and an absent relay fails closed instead of degrading
    # deterministic forecasts into fake probabilities.
    for offset in range(0, len(stations), 10):
        batch = stations[offset : offset + 10]
        query = urlencode(
            {
                "latitude": ",".join(str(station["latitude"]) for station in batch),
                "longitude": ",".join(str(station["longitude"]) for station in batch),
                "daily": "temperature_2m_max,temperature_2m_min",
                "temperature_unit": "fahrenheit",
                "timezone": "auto",
                "forecast_days": 4,
                "models": "gfs_seamless,ecmwf_ifs025",
            },
            safe=",",
        )
        try:
            payload = fetch_json(
                f"https://ensemble-api.open-meteo.com/v1/ensemble?{query}"
            )
            responses = payload if isinstance(payload, list) else [payload]
            for index, station in enumerate(batch):
                response = responses[index] if index < len(responses) and isinstance(responses[index], dict) else {}
                daily = response.get("daily") if isinstance(response.get("daily"), dict) else {}
                dates = daily.get("time") if isinstance(daily.get("time"), list) else []
                families: set[str] = set()
                grouped: dict[tuple[str, str], list[float]] = {}
                for field, raw_values in daily.items():
                    match = re.fullmatch(
                        r"(temperature_2m_(?:max|min))(?:_member\d+)?_(ncep_gefs_seamless|ecmwf_ifs025_ensemble)",
                        str(field),
                    )
                    if not match or not isinstance(raw_values, list):
                        continue
                    families.add(match.group(2))
                    for day_index, raw_value in enumerate(raw_values[:4]):
                        if day_index >= len(dates) or not re.fullmatch(r"20\d{2}-\d{2}-\d{2}", str(dates[day_index])):
                            continue
                        value = as_float(raw_value)
                        if value is None or value < -150 or value > 160:
                            continue
                        grouped.setdefault((match.group(1), str(dates[day_index])), []).append(value)
                histograms = []
                for (field, date), values in sorted(grouped.items()):
                    counts: dict[float, int] = {}
                    for value in values:
                        rounded = round(value, 1)
                        counts[rounded] = counts.get(rounded, 0) + 1
                    histograms.append(
                        {"field": field, "date": date, "values": sorted(counts.items())}
                    )
                if len(families) >= 2 and histograms:
                    ensembles_by_station[str(station["stationId"])] = {
                        "familyIds": sorted(families),
                        "histograms": histograms,
                    }
                else:
                    failures.append(f"{station['stationId']}/ensemble: fewer than two usable families")
        except Exception as error:
            failures.append(f"ensemble batch {offset // 10 + 1}: {str(error)[:180]}")
        if offset + 10 < len(stations):
            time.sleep(1.0)

    forecasts = []
    for ticker, station in series_stations:
        models = models_by_station.get(str(station["stationId"]), [])
        if not models:
            failures.append(f"{ticker}: no forecast models returned")
            continue
        forecasts.append(
            {
                "seriesTicker": ticker,
                "stationId": station["stationId"],
                "latitude": station["latitude"],
                "longitude": station["longitude"],
                "models": models,
                "ensemble": ensembles_by_station.get(str(station["stationId"])),
            }
        )
    return forecasts, failures


def collect_climate_catalog() -> dict[str, Any]:
    captured_at = minute_timestamp()
    batch_index, tickers = climate_rotation()
    relayed_series: list[dict[str, Any]] = []
    failures: list[str] = []
    for index, ticker in enumerate(tickers):
        try:
            series = fetch_climate_series(ticker)
            if series["markets"]:
                relayed_series.append(series)
            else:
                failures.append(f"{ticker}: no open markets")
        except Exception as error:
            failures.append(f"{ticker}: {error}")
        if index + 1 < len(tickers):
            time.sleep(0.25)

    forecasts, forecast_failures = collect_climate_forecasts(relayed_series)
    response = post_json(
        "/api/climate-catalog-ingest",
        {
            "source": "github-actions-kalshi",
            "capturedAt": captured_at,
            "candidateSeries": len(CLIMATE_DIRECTORY_SERIES),
            "scheduledSeries": len(tickers),
            "batchIndex": batch_index,
            "batchCount": CLIMATE_BATCH_COUNT,
            "failures": failures,
            "forecastFailures": forecast_failures,
            "forecasts": forecasts,
            "series": relayed_series,
        },
        timeout=300,
    )
    results = response.get("results") if isinstance(response.get("results"), list) else []
    result = results[0] if results and isinstance(results[0], dict) else {}
    if response.get("accepted") is not True or result.get("status") != "SUCCEEDED":
        raise RuntimeError(str(result.get("error") or response.get("reason") or "Climate ingest failed"))

    settlement_stats, settlement_warnings = collect_targeted_settlements(["CLIMATE"])
    post_batches("stat", settlement_stats)
    return {
        "source": "climate",
        "status": "SUCCEEDED",
        "rotation": f"{batch_index + 1}/{CLIMATE_BATCH_COUNT}",
        "scheduledSeries": len(tickers),
        "relayedSeries": len(relayed_series),
        "forecastSeries": len(forecasts),
        "recordsWritten": int(response.get("recordsWritten") or 0),
        "settlementRecords": len(settlement_stats),
        "warnings": failures + forecast_failures + settlement_warnings,
    }


def collect_targeted_settlements(
    sports: list[str],
) -> tuple[list[dict[str, Any]], list[str]]:
    captured_at = minute_timestamp()
    records: list[dict[str, Any]] = []
    warnings: list[str] = []
    try:
        target_payload = securus_json("/api/paper-settlement-targets")
    except Exception as error:
        return records, [f"settlement targets: {error}"]

    for raw_target in target_payload.get("targets", []):
        target = raw_target if isinstance(raw_target, dict) else {}
        sport = str(target.get("sport") or "").upper()
        ticker = str(target.get("ticker") or "")
        event_key = str(target.get("eventKey") or "")
        market_key = str(target.get("market") or "")
        if sport not in sports or not ticker or not event_key or not market_key:
            continue
        try:
            api_base = CLIMATE_API_BASE if sport == "CLIMATE" else API_BASE
            response = fetch_json(f"{api_base}/markets/{quote(ticker, safe='')}")
            market = response.get("market") or {}
            result = str(market.get("result") or "").lower()
            status = str(market.get("status") or "").lower()
            settlement_timestamp = str(
                market.get("settlement_ts")
                or market.get("settlement_time")
                or market.get("settled_time")
                or ""
            )
            settlement_value = as_float(market.get("settlement_value_dollars"))
            if (
                result not in {"yes", "no", "scalar"}
                or status not in {"finalized", "settled"}
                or parse_timestamp(settlement_timestamp) is None
                or (
                    result == "scalar"
                    and (settlement_value is None or not 0 <= settlement_value <= 1)
                )
                or (
                    result in {"yes", "no"}
                    and settlement_value is not None
                    and abs(settlement_value - (1 if result == "yes" else 0)) > 0.000001
                )
            ):
                continue
            title = str(market.get("title") or market_key)
            updated_time = str(market.get("updated_time") or captured_at)
            metadata = {
                "ticker": ticker,
                "marketKey": market_key,
                "title": title,
                "yesSubTitle": str(market.get("yes_sub_title") or "yes"),
                "noSubTitle": str(market.get("no_sub_title") or "no"),
                "status": status,
                "result": result,
                "settlementValueDollars": settlement_value,
                "settlementTimestamp": settlement_timestamp,
                "closeTime": str(market.get("close_time") or ""),
                "expectedExpirationTime": str(market.get("expected_expiration_time") or ""),
                "expirationTime": str(market.get("expiration_time") or ""),
                "latestExpirationTime": str(market.get("latest_expiration_time") or ""),
                "updatedTime": updated_time,
            }
            records.append(
                {
                    "source": SOURCE_ID,
                    "sport": sport,
                    "recordType": "kalshi-market",
                    "recordKey": f"kalshi-market:{ticker}:settled",
                    "eventKey": event_key,
                    "payload": metadata,
                    "observedAt": settlement_timestamp,
                    "capturedAt": captured_at,
                    "dedupeKey": json.dumps(metadata, sort_keys=True, separators=(",", ":")),
                }
            )
        except Exception as error:
            warnings.append(f"settlement {ticker}: {error}")
    return records, warnings

def main() -> int:
    started_at = utc_now()
    requested_sport = os.environ.get("KEYLESS_SPORT", "ALL").upper()
    if requested_sport == "CLIMATE":
        try:
            result = collect_climate_catalog()
            print(json.dumps(result))
            return 0
        except Exception as error:
            print(str(error)[:500], file=sys.stderr)
            return 1
    if requested_sport in ("", "ALL"):
        sports = list(SERIES)
    elif requested_sport in SERIES:
        sports = [requested_sport]
    else:
        print(f"Unsupported Kalshi sport: {requested_sport}", file=sys.stderr)
        return 2

    try:
        all_odds: list[dict[str, Any]] = []
        all_stats: list[dict[str, Any]] = []
        warnings: list[str] = []
        failures: list[str] = []

        for sport in sports:
            try:
                odds, stats, sport_warnings = collect_sport(sport)
                all_odds.extend(odds)
                all_stats.extend(stats)
                warnings.extend(sport_warnings)
            except Exception as error:
                failures.append(f"{sport}: {error}")

        settlement_stats, settlement_warnings = collect_targeted_settlements(sports)
        all_stats.extend(settlement_stats)
        warnings.extend(settlement_warnings)

        if failures:
            raise RuntimeError("; ".join(failures))

        post_batches("odds", all_odds)
        post_batches("stat", all_stats)
        records_written = len(all_odds) + len(all_stats)
        warning = "; ".join(warnings) or None
        sync_run("SUCCEEDED", records_written, started_at, warning)
        print(
            json.dumps(
                {
                    "source": SOURCE_ID,
                    "sports": sports,
                    "status": "SUCCEEDED",
                    "oddsRecords": len(all_odds),
                    "metadataRecords": len(all_stats),
                    "warning": warning,
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

#!/usr/bin/env python3
"""Transport raw public Kalshi prop documents to the private Securus parser.

No models, matching rules, betting decisions, or credentials are stored here.
One fixed API host, bounded responses, sequential requests, and no retries.
"""
from datetime import datetime, timezone
import json
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

if __package__:
    from .collect_kalshi import API_BASE, USER_AGENT, post_json as securus_post
else:
    from collect_kalshi import API_BASE, USER_AGENT, post_json as securus_post

SERIES = {
    "MLB": ("KXMLBKS", "KXMLBHR"),
    "NFL": ("KXNFLPASSYDS", "KXNFLRSHYDS", "KXNFLRECYDS", "KXNFLREC", "KXNFLPASSTDS"),
}
MAX_BYTES = 8_000_000


def diagnostic(stage, code, **counts):
    """Only fixed transport codes and public-response counts; never exception text."""
    print(json.dumps({"source": "kalshi-props", "stage": stage, "code": code, **counts}), file=sys.stderr)


class OfficialTransport:
    def __init__(self):
        self.blocked = False
        self.last_request = 0.0

    def get(self, path):
        if self.blocked:
            raise RuntimeError("upstream unavailable in this cycle")
        time.sleep(max(0.0, 1.0 - (time.monotonic() - self.last_request)))
        self.last_request = time.monotonic()
        try:
            request = Request(API_BASE + path, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
            with urlopen(request, timeout=20) as response:
                body = response.read(MAX_BYTES + 1)
                if len(body) > MAX_BYTES:
                    raise ValueError("oversized upstream document")
                data = json.loads(body)
                if not isinstance(data, dict):
                    raise ValueError("invalid upstream document")
                return data
        except Exception as error:
            # A rate limit or other failure stops provider requests for the rest
            # of this cycle, across both sports. Never switch hosts or identities.
            self.blocked = True
            endpoint = "events" if path.startswith("/events?") else "targets" if path.startswith("/structured_targets?") else "series"
            code = f"HTTP_{error.code}" if isinstance(error, HTTPError) else "TIMEOUT" if isinstance(error, TimeoutError) else "NETWORK" if isinstance(error, URLError) else "OVERSIZED_DOCUMENT" if isinstance(error, ValueError) and str(error) == "oversized upstream document" else "INVALID_DOCUMENT"
            diagnostic(endpoint, code)
            raise


def capture(sport, transport, series_ticker=None):
    if series_ticker is not None and series_ticker not in SERIES[sport]:
        raise ValueError("unsupported series")
    requested_series = (series_ticker,) if series_ticker else SERIES[sport]
    captured_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    entries = []
    targets = set()
    target_ids_by_ticker = {}
    for ticker in requested_series:
        try:
            series = transport.get("/series/" + ticker)
            events = transport.get("/events?" + urlencode({
                "series_ticker": ticker, "status": "open", "limit": "200",
                "with_nested_markets": "true", "with_milestones": "true",
            }))
            if not isinstance(events.get("events"), list) or not isinstance(events.get("milestones"), list):
                raise ValueError("missing official event envelope")
            entry_targets = set()
            for milestone in events["milestones"]:
                details = milestone.get("details") or {}
                entry_targets.update(str(details[key]) for key in ("away_team_id", "home_team_id") if details.get(key))
            for event in events["events"]:
                for market in event.get("markets", []):
                    strike = market.get("custom_strike") or {}
                    prefix = "baseball" if sport == "MLB" else "football"
                    entry_targets.update(str(strike[key]) for key in (prefix + "_player", prefix + "_team") if strike.get(key))
            targets.update(entry_targets)
            target_ids_by_ticker[ticker] = entry_targets
            entries.append({"ticker": ticker, "series": series, "events": events})
        except Exception:
            entries.append({"ticker": ticker, "error": "UPSTREAM_UNAVAILABLE"})
    documents = []
    try:
        ids = sorted(targets)
        for offset in range(0, len(ids), 150):
            query = urlencode([("ids", target) for target in ids[offset:offset + 150]] + [("page_size", "200")])
            data = transport.get("/structured_targets?" + query)
            if not isinstance(data.get("structured_targets"), list):
                raise ValueError("missing official target envelope")
            documents.extend(data["structured_targets"])
        for entry in entries:
            if "error" not in entry:
                ids = target_ids_by_ticker.get(entry["ticker"], set())
                entry["targets"] = {"structured_targets": [
                    document for document in documents
                    if isinstance(document, dict) and str(document.get("id")) in ids
                ]}
    except Exception:
        entries = [{"ticker": ticker, "error": "UPSTREAM_UNAVAILABLE"} for ticker in requested_series]
    payload = {"sport": sport, "capturedAt": captured_at, "series": entries}
    if series_ticker:
        payload["scope"] = "SERIES"
    payload_bytes = len(json.dumps(payload, separators=(",", ":")).encode())
    if payload_bytes > MAX_BYTES:
        diagnostic("snapshot", "OVERSIZED_SNAPSHOT", sport=sport, bytes=payload_bytes)
        payload["series"] = [{"ticker": ticker, "error": "UPSTREAM_UNAVAILABLE"} for ticker in requested_series]
    return payload


def main():
    transport = OfficialTransport()
    failed = False
    for sport in SERIES:
        for ticker in SERIES[sport]:
            try:
                # Send each original series envelope separately, including only
                # its own referenced targets. The receiver's 8 MB bound and the
                # real capture time remain unchanged; there is no full-slate POST.
                payload = capture(sport, transport, ticker)
                result = securus_post("/api/kalshi-props-ingest", payload, timeout=90, attempts=1)
                ok = result.get("accepted") is True and len(result.get("results", [])) == 1 and result["results"][0].get("status") == "SUCCEEDED"
                failed |= not ok
                print(json.dumps({"source": "kalshi-props", "sport": sport, "series": ticker, "status": "SUCCEEDED" if ok else "FAILED"}))
            except Exception:
                failed = True
                print(json.dumps({"source": "kalshi-props", "sport": sport, "series": ticker, "status": "FAILED"}))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Transport one bounded official public NBA scoreboard; no model or decision logic."""
from datetime import datetime, timezone
import json
import sys
from urllib.request import Request, urlopen

if __package__:
    from .collect_kalshi import USER_AGENT, post_json
else:
    from collect_kalshi import USER_AGENT, post_json

URL = "https://cdn.nba.com/static/json/liveData/scoreboard/todaysScoreboard_00.json"
MAX_BYTES = 500_000


def capture():
    captured = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    with urlopen(Request(URL, headers={"Accept": "application/json", "User-Agent": USER_AGENT}), timeout=20) as response:
        raw = response.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("oversized scoreboard")
    data = json.loads(raw)
    if not isinstance(data, dict) or not isinstance(data.get("scoreboard"), dict) or not isinstance(data.get("meta"), dict):
        raise ValueError("invalid scoreboard")
    return {"capturedAt": captured, "meta": data["meta"], "scoreboard": data["scoreboard"]}


def main():
    try:
        result = post_json("/api/nba-scoreboard-ingest", capture(), timeout=90, attempts=1)
        ok = result.get("accepted") is True and len(result.get("results", [])) == 1 and result["results"][0].get("status") == "SUCCEEDED"
    except Exception:
        ok = False
    print(json.dumps({"source": "nba-stats", "status": "SUCCEEDED" if ok else "FAILED"}))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

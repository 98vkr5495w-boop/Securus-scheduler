from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib.util
import pathlib
import unittest


SCRIPT_PATH = pathlib.Path(__file__).parents[1] / "scripts" / "verify_freshness.py"
SPEC = importlib.util.spec_from_file_location("verify_freshness", SCRIPT_PATH)
assert SPEC and SPEC.loader
verify_freshness = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verify_freshness)


NOW = datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc)
OFFSEASON_NOW = datetime(2026, 9, 2, 18, 0, tzinfo=timezone.utc)


def iso(minutes_ago: int) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).isoformat().replace("+00:00", "Z")


def source_row(source_id: str, minutes_ago: int = 5, status: str = "SUCCEEDED") -> dict:
    return {
        "id": source_id,
        "lastRun": {"status": status, "completedAt": iso(minutes_ago)},
    }


def payload(overrides: dict[str, dict] | None = None, capacity_state: str = "WARNING") -> dict:
    sources = {
        source: source_row(source)
        for requirements in verify_freshness.MARKET_REQUIREMENTS.values()
        for source, _ in requirements
    }
    sources.update(overrides or {})
    return {
        "sources": list(sources.values()),
        "storage": {"capacity": {"capacityState": capacity_state}},
    }


class FreshnessGateTests(unittest.TestCase):
    def test_market_table_matches_the_site_requirements(self) -> None:
        self.assertEqual(
            set(verify_freshness.MARKET_REQUIREMENTS),
            {"MLB", "NFL", "NBA", "CLIMATE"},
        )
        self.assertEqual(dict(verify_freshness.MARKET_REQUIREMENTS["CLIMATE"]), {"climate": 60})
        self.assertEqual(dict(verify_freshness.MARKET_REQUIREMENTS["NBA"])["espn"], 180)
        self.assertEqual(dict(verify_freshness.MARKET_REQUIREMENTS["MLB"])["kalshi"], 45)
        self.assertEqual(dict(verify_freshness.MARKET_REQUIREMENTS["NFL"])["nflverse"], 26 * 60)

    def test_every_market_is_fresh_when_all_sources_are_recent(self) -> None:
        results = verify_freshness.evaluate_markets(payload(), NOW)
        self.assertEqual(results, {"MLB": [], "NFL": [], "NBA": [], "CLIMATE": []})

    def test_a_supplemental_feed_outage_withholds_only_its_market(self) -> None:
        results = verify_freshness.evaluate_markets(
            payload({"espn": source_row("espn", status="FAILED")}), NOW
        )
        self.assertEqual(results["NBA"], ["espn unhealthy"])
        self.assertEqual(results["MLB"], [])
        self.assertEqual(results["NFL"], [])
        self.assertEqual(results["CLIMATE"], [])

    def test_a_shared_source_outage_withholds_every_dependent_market(self) -> None:
        results = verify_freshness.evaluate_markets(
            payload({"open-meteo": source_row("open-meteo", status="FAILED")}), NOW
        )
        self.assertEqual(results["MLB"], ["open-meteo unhealthy"])
        self.assertEqual(results["NFL"], ["open-meteo unhealthy"])
        self.assertEqual(results["NBA"], [])
        self.assertEqual(results["CLIMATE"], [])

    def test_stale_and_missing_sources_are_reported_distinctly(self) -> None:
        stale = payload({"kalshi": source_row("kalshi", minutes_ago=46)})
        stale["sources"] = [row for row in stale["sources"] if row["id"] != "climate"]
        results = verify_freshness.evaluate_markets(stale, NOW)
        self.assertIn("kalshi stale", results["MLB"])
        self.assertIn("kalshi stale", results["NFL"])
        self.assertIn("kalshi stale", results["NBA"])
        self.assertEqual(results["CLIMATE"], ["climate missing"])

    def test_unparseable_or_naive_timestamps_fail_closed(self) -> None:
        naive = {"id": "climate", "lastRun": {"status": "SUCCEEDED", "completedAt": "2026-10-15T17:55:00"}}
        garbage = {"id": "kalshi", "lastRun": {"status": "SUCCEEDED", "completedAt": "soon"}}
        results = verify_freshness.evaluate_markets(
            payload({"climate": naive, "kalshi": garbage}), NOW
        )
        self.assertEqual(results["CLIMATE"], ["climate unhealthy"])
        self.assertIn("kalshi unhealthy", results["MLB"])

    def test_nba_is_skipped_during_the_offseason(self) -> None:
        results = verify_freshness.evaluate_markets(
            payload({"espn": source_row("espn", status="FAILED")}), OFFSEASON_NOW
        )
        self.assertNotIn("NBA", results)
        self.assertEqual(results["MLB"], [])

    def test_market_filter_limits_the_evaluation(self) -> None:
        results = verify_freshness.evaluate_markets(payload(), NOW, ["CLIMATE"])
        self.assertEqual(results, {"CLIMATE": []})

    def test_storage_critical_is_detected(self) -> None:
        self.assertTrue(verify_freshness.storage_is_critical(payload(capacity_state="CRITICAL")))
        self.assertFalse(verify_freshness.storage_is_critical(payload(capacity_state="WARNING")))
        self.assertFalse(verify_freshness.storage_is_critical({}))


if __name__ == "__main__":
    unittest.main()

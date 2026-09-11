from datetime import datetime, timedelta, timezone
import unittest

from scripts.verify_freshness import freshness_failures, verify_with_rechecks


NOW = datetime(2026, 10, 1, 16, 0, tzinfo=timezone.utc)


def payload(records_written):
    return {
        "sources": [{
            "id": "nba-official-injuries",
            "lastRun": {
                "status": "SUCCEEDED",
                "recordsWritten": records_written,
                "completedAt": "2026-10-01T15:59:00Z",
            },
        }],
        "storage": {"capacity": {"capacityState": "NORMAL", "utilizationPercent": 50}},
    }


class FreshnessTests(unittest.TestCase):
    def test_cached_stale_snapshot_rechecks_without_changing_age_budget(self):
        stale = payload(1)
        stale["sources"][0]["id"] = "climate"
        stale["sources"][0]["lastRun"]["completedAt"] = "2026-10-01T14:00:00Z"
        fresh = payload(1)
        fresh["sources"][0]["id"] = "climate"
        snapshots = iter([stale, stale, stale, fresh])
        waits = []
        self.assertEqual(verify_with_rechecks(lambda: next(snapshots), {"climate"},
            clock=lambda: NOW, sleep=waits.append), [])
        self.assertEqual(waits, [15, 15, 15])
        waits.clear()
        failures = verify_with_rechecks(lambda: stale, {"climate"}, clock=lambda: NOW, sleep=waits.append)
        self.assertIn("climate stale", failures[0])
        self.assertIn("2026-10-01T14:00:00Z", failures[0])
        self.assertEqual(waits, [15, 15, 15])

    def test_future_timestamps_are_not_fresh(self):
        state = payload(1)
        state["sources"][0]["lastRun"]["completedAt"] = (NOW + timedelta(minutes=1)).isoformat()
        self.assertIn("future completion", freshness_failures(state, {"nba-official-injuries"}, NOW)[0])

    def test_critical_or_unknown_storage_never_passes_or_retries(self):
        for capacity in [{}, {"capacityState": "NORMAL", "utilizationPercent": 85},
                         {"capacityState": "CRITICAL", "utilizationPercent": 80},
                         {"capacityState": "NORMAL", "utilizationPercent": float('nan')}]:
            state = payload(1)
            state["storage"]["capacity"] = capacity
            waits = []
            failures = verify_with_rechecks(lambda: state, {"nba-official-injuries"}, clock=lambda: NOW, sleep=waits.append)
            self.assertTrue(any(x.startswith("storage") for x in failures))
            self.assertEqual(waits, [])

    def test_active_season_requires_a_verified_report_record(self):
        wanted = {"nba-official-injuries"}
        self.assertEqual(
            freshness_failures(payload(0), wanted, NOW),
            ["nba-official-injuries has no verified active-season report"],
        )
        self.assertEqual(freshness_failures(payload(1), wanted, NOW), [])


if __name__ == "__main__":
    unittest.main()

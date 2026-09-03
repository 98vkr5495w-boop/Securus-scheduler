from datetime import datetime, timezone
import unittest

from scripts.verify_freshness import freshness_failures


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
        "storage": {"capacity": {"capacityState": "NORMAL"}},
    }


class FreshnessTests(unittest.TestCase):
    def test_active_season_requires_a_verified_report_record(self):
        wanted = {"nba-official-injuries"}
        self.assertEqual(
            freshness_failures(payload(0), wanted, NOW),
            ["nba-official-injuries has no verified active-season report"],
        )
        self.assertEqual(freshness_failures(payload(1), wanted, NOW), [])


if __name__ == "__main__":
    unittest.main()

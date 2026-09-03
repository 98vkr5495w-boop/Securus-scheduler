import unittest
import uuid

from scripts.run_crypto_paper import deterministic_run_id, run_and_verify


class DeterministicRunIdTests(unittest.TestCase):
    def test_same_repository_and_cycle_reuse_one_valid_uuid4(self):
        first = deterministic_run_id("1350501140", "2026-09-03T05:07:00Z")
        second = deterministic_run_id("1350501140", "2026-09-03T05:07:00Z")
        self.assertEqual(first, second)
        parsed = uuid.UUID(first)
        self.assertEqual(parsed.version, 4)
        self.assertEqual(parsed.variant, uuid.RFC_4122)

    def test_adjacent_cycles_differ(self):
        first = deterministic_run_id("1350501140", "2026-09-03T05:07:00Z")
        second = deterministic_run_id("1350501140", "2026-09-03T05:37:00Z")
        self.assertNotEqual(first, second)

    def test_repository_identity_is_part_of_the_key(self):
        self.assertNotEqual(
            deterministic_run_id("repository-a", "2026-09-03T05:07:00Z"),
            deterministic_run_id("repository-b", "2026-09-03T05:07:00Z"),
        )

    def test_existing_completed_request_is_verified_without_replacement(self):
        run_id = "59d01491-899d-4e25-9d29-2d1776e35ac0"
        lifecycle = {
            "accepted": True,
            "created": False,
            "runId": run_id,
            "triggerName": "github-actions-paper-scan",
            "mode": "PAPER_ONLY",
            "status": "SUCCEEDED",
            "requestedAt": "2026-09-03T04:59:00.000Z",
            "startedAt": "2026-09-03T05:01:00.000Z",
            "completedAt": "2026-09-03T05:03:00.000Z",
            "sourceWatermark": {
                "version": 1,
                "capturedAt": "2026-09-03T05:00:00.000Z",
                "sourceSyncRunId": 1,
                "oddsSnapshotId": 2,
                "playerPropSnapshotId": 3,
                "statSnapshotId": 4,
            },
            "result": {
                "accepted": True,
                "mode": "PAPER_ONLY",
                "analyst": "SECURUS",
                "paperBettor": "CRYPTO",
                "venue": "KALSHI",
                "realMoneyExecution": False,
                "startedAt": "2026-09-03T05:01:00.000Z",
                "completedAt": "2026-09-03T05:02:00.000Z",
                "errors": [],
                "decisions": [],
            },
        }
        calls = []

        def loader(url, **kwargs):
            calls.append((url, kwargs.get("method", "GET")))
            return lifecycle

        report = run_and_verify(
            "https://example.test",
            poll_attempts=1,
            poll_delay_seconds=0,
            run_id=run_id,
            loader=loader,
            sleeper=lambda _seconds: None,
        )
        self.assertEqual(report["status"], "VERIFIED")
        self.assertEqual(report["runId"], run_id)
        self.assertEqual([method for _, method in calls], ["POST", "GET"])


if __name__ == "__main__":
    unittest.main()

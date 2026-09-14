import unittest
import uuid

from scripts.run_crypto_paper import (
    DEFAULT_POLL_ATTEMPTS,
    DEFAULT_POLL_DELAY_SECONDS,
    deterministic_run_id,
    failure_category,
    run_and_verify,
)


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

    def lifecycle(self, status, **overrides):
        base = {
            "accepted": True, "created": True, "runId": "59d01491-899d-4e25-9d29-2d1776e35ac0",
            "triggerName": "github-actions-paper-scan", "mode": "PAPER_ONLY", "status": status,
            "requestedAt": "2026-09-13T17:45:25.000Z", "startedAt": "2026-09-13T17:45:26.000Z",
            "completedAt": None, "result": None, "error": None,
            "sourceWatermark": {"version": 1, "capturedAt": "2026-09-13T17:45:25.500Z",
                                "sourceSyncRunId": 1, "oddsSnapshotId": 2,
                                "playerPropSnapshotId": 3, "statSnapshotId": 4},
        }
        base.update(overrides)
        return base

    def test_polling_outlasts_the_site_abandonment_horizon(self):
        # A worker killed after the 202 is projected TIMED_OUT by Securus no later
        # than 30 minutes after its start; 5 minutes of polling hid that cause on
        # 2026-09-12/13 as an ambiguous "failed verification".
        self.assertGreaterEqual((DEFAULT_POLL_ATTEMPTS - 1) * DEFAULT_POLL_DELAY_SECONDS, 31 * 60)

    def test_dead_worker_is_reported_as_timed_out_not_as_unverified(self):
        run_id = "59d01491-899d-4e25-9d29-2d1776e35ac0"
        polls = []
        def loader(url, **kwargs):
            if kwargs.get("method") == "POST":
                return self.lifecycle("RUNNING")
            polls.append(url)
            return self.lifecycle("RUNNING") if len(polls) < 3 else self.lifecycle(
                "TIMED_OUT", completedAt="2026-09-13T18:00:26.000Z",
                error="Paper worker lease expired without a terminal result; outcome unverified, run will not be replayed")
        with self.assertRaises(RuntimeError) as raised:
            run_and_verify("https://example.test", poll_attempts=10, poll_delay_seconds=0,
                           run_id=run_id, loader=loader, sleeper=lambda _s: None)
        self.assertEqual(failure_category(raised.exception), "RUN_TIMED_OUT")
        self.assertEqual(len(polls), 3, "polling must stop at the first terminal status")
        self.assertNotIn("market", str(raised.exception).lower())

    def test_nonterminal_run_never_becomes_a_verified_scan(self):
        run_id = "59d01491-899d-4e25-9d29-2d1776e35ac0"
        def loader(url, **kwargs):
            return self.lifecycle("RUNNING")
        with self.assertRaises(RuntimeError) as raised:
            run_and_verify("https://example.test", poll_attempts=4, poll_delay_seconds=0,
                           run_id=run_id, loader=loader, sleeper=lambda _s: None)
        self.assertEqual(failure_category(raised.exception), "RUN_NOT_TERMINAL")

    def test_failure_categories_stay_coarse_and_non_diagnostic(self):
        cases = {
            RuntimeError("Crypto paper run FAILED: Cleveland wins [KX...] rejected"): "RUN_FAILED",
            RuntimeError("Crypto paper run BLOCKED: storage"): "RUN_BLOCKED",
            RuntimeError("Crypto paper scan verification failed: result.errors=['x']"): "RESULT_INVALID",
            RuntimeError("Crypto paper run result timestamps are out of order"): "RESULT_INVALID",
            RuntimeError("HTTP 401: nope"): "REQUEST_REJECTED",
            RuntimeError("request failed after 3 attempts: timed out"): "TRANSPORT",
        }
        for error, expected in cases.items():
            self.assertEqual(failure_category(error), expected, str(error))
            self.assertNotIn("Cleveland", expected)


if __name__ == "__main__":
    unittest.main()

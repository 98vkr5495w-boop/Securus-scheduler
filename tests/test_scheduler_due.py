from datetime import datetime, timezone
import unittest

from scripts.check_scheduler_due import (
    cycle_start,
    cycle_after_grace,
    format_cycle_key,
    latest_slot,
    recover_cycle,
    scheduler_is_due,
)


CYCLE = datetime(2026, 9, 3, 5, 7, tzinfo=timezone.utc)


def fake_api(
    *,
    job_name="collect-and-scan",
    conclusion="success",
    started_at="2026-09-03T05:08:00Z",
    completed_at="2026-09-03T05:12:00Z",
):
    def load(path):
        if "/runs?" in path:
            return {"workflow_runs": [{"id": 42}]}
        return {"jobs": [{
            "name": job_name,
            "conclusion": conclusion,
            "started_at": started_at,
            "completed_at": completed_at,
        }]}
    return load


class SchedulerDueTests(unittest.TestCase):
    def check(self, api):
        return scheduler_is_due(
            api,
            "owner/repo",
            "securus-scheduler.yml",
            "main",
            CYCLE,
        )

    def test_recent_full_job_is_fresh(self):
        self.assertEqual(self.check(fake_api())[0], False)

    def test_skipped_collect_job_does_not_count(self):
        self.assertEqual(self.check(fake_api(conclusion="skipped"))[0], True)

    def test_other_successful_job_does_not_count(self):
        self.assertEqual(self.check(fake_api(job_name="validate"))[0], True)

    def test_stale_or_failed_collection_is_due(self):
        self.assertEqual(self.check(fake_api(completed_at="2026-09-03T04:50:00Z"))[0], True)
        self.assertEqual(self.check(fake_api(conclusion="failure"))[0], True)
        self.assertEqual(self.check(fake_api(conclusion=None, completed_at=None))[0], True)

    def test_api_errors_run_fail_safe(self):
        def failed(_path):
            raise RuntimeError("unavailable")
        due, reason = self.check(failed)
        self.assertEqual(due, True)
        self.assertIn("could not be verified", reason)

    def test_boundaries_are_anchored_at_07_and_37(self):
        expected = {
            (5, 29): "2026-09-03T05:07:00Z",
            (5, 30): "2026-09-03T05:07:00Z",
            (5, 36): "2026-09-03T05:07:00Z",
            (5, 37): "2026-09-03T05:37:00Z",
        }
        for (hour, minute), cycle in expected.items():
            with self.subTest(hour=hour, minute=minute):
                now = datetime(2026, 9, 3, hour, minute, tzinfo=timezone.utc)
                self.assertEqual(format_cycle_key(cycle_start(now)), cycle)

    def test_rolling_cycle_and_delayed_primary_resolve_expected_boundaries(self):
        self.assertEqual(
            cycle_start(datetime(2026, 9, 3, 5, 31, tzinfo=timezone.utc)),
            CYCLE,
        )
        self.assertEqual(
            latest_slot(
                datetime(2026, 9, 3, 5, 38, tzinfo=timezone.utc),
                7,
            ),
            CYCLE,
            "an unusually delayed :07 primary retains its explicit cron cycle",
        )
        self.assertEqual(
            cycle_start(datetime(2026, 9, 3, 5, 38, tzinfo=timezone.utc)),
            datetime(2026, 9, 3, 5, 37, tzinfo=timezone.utc),
        )
        self.assertEqual(
            cycle_after_grace(
                datetime(2026, 9, 3, 6, 5, tzinfo=timezone.utc),
                15,
            ),
            datetime(2026, 9, 3, 5, 37, tzinfo=timezone.utc),
            "a delayed watchdog checks the newest cycle whose grace elapsed",
        )

    def test_only_a_success_at_or_after_the_cycle_counts(self):
        before = fake_api(started_at="2026-09-03T05:06:59Z", completed_at="2026-09-03T05:06:59Z")
        boundary = fake_api(started_at="2026-09-03T05:07:00Z", completed_at="2026-09-03T05:07:00Z")
        self.assertEqual(self.check(before)[0], True)
        self.assertEqual(self.check(boundary)[0], False)
        self.assertEqual(
            self.check(fake_api(
                started_at="2026-09-03T05:06:00Z",
                completed_at="2026-09-03T05:12:00Z",
            ))[0],
            False,
            "a straddling successful collection satisfies the newer freshness cycle",
        )

    def test_recovery_dispatch_carries_the_exact_cycle_and_skips_fresh_cycles(self):
        dispatched = []

        def request(path, **kwargs):
            dispatched.append((path, kwargs))
            return {}

        due, _ = recover_cycle(
            fake_api(started_at="2026-09-03T05:06:59Z", completed_at="2026-09-03T05:06:59Z"),
            request,
            "owner/repo",
            "securus-scheduler.yml",
            "main",
            CYCLE,
        )
        self.assertTrue(due)
        self.assertEqual(dispatched, [(
            "/repos/owner/repo/actions/workflows/securus-scheduler.yml/dispatches",
            {
                "method": "POST",
                "payload": {
                    "ref": "main",
                    "inputs": {
                        "recovery": "true",
                        "cycle_key": "2026-09-03T05:07:00Z",
                    },
                },
            },
        )])

        dispatched.clear()
        due, _ = recover_cycle(
            fake_api(started_at="2026-09-03T05:07:00Z", completed_at="2026-09-03T05:07:00Z"),
            request,
            "owner/repo",
            "securus-scheduler.yml",
            "main",
            CYCLE,
        )
        self.assertFalse(due)
        self.assertEqual(dispatched, [])


if __name__ == "__main__":
    unittest.main()

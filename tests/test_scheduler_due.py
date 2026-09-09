from datetime import datetime, timedelta, timezone
from unittest.mock import patch
import unittest

from scripts.check_scheduler_due import (
    cycle_start,
    cycle_after_grace,
    format_cycle_key,
    latest_slot,
    recover_cycle,
    scheduler_is_due,
    effective_cycle,
    source_refresh_due,
    read_public_source_status,
    FREQUENT_SOURCES,
    DEEP_SOURCES,
    deep_refresh_due,
)


CYCLE = datetime(2026, 9, 3, 5, 7, tzinfo=timezone.utc)


def fake_api(
    *,
    job_name="collect-inputs",
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

    def test_successful_scan_only_rerun_does_not_refresh_collection_clock(self):
        self.assertTrue(self.check(fake_api(job_name="collect-and-scan"))[0])

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

    def sources(self, now, age=0):
        return {"sources": [{"id": source, "lastRun": {
            "status": "SUCCEEDED", "completedAt": format_cycle_key(now - timedelta(minutes=age)),
        }} for source in FREQUENT_SOURCES | DEEP_SOURCES]}

    def test_actual_sources_not_a_green_workflow_determine_refresh(self):
        now = CYCLE + timedelta(minutes=70)
        boundary = cycle_start(now)
        for age, expected in [(0, False), (29, False), (30, True), (46, True), (-1, True)]:
            with self.subTest(age=age):
                due, _ = scheduler_is_due(fake_api(), "owner/repo", "securus-scheduler.yml", "main",
                    boundary, now=now, source_get=lambda: self.sources(now, age))
                self.assertEqual(due, expected)
        payload = self.sources(now)
        payload["paperBetReadiness"] = {"status": "NO_BET", "readySports": []}
        self.assertFalse(source_refresh_due(payload, now),
            "a betting-policy abstention must never cause a collection retry storm")

    def test_missing_failed_and_incomplete_source_records_need_refresh(self):
        now = CYCLE + timedelta(minutes=70)
        for payload in [{}, {"sources": None}, {"sources": []},
                        {"sources": [{"id": source} for source in FREQUENT_SOURCES]}]:
            self.assertTrue(source_refresh_due(payload, now))

    def test_deep_sources_cannot_starve_when_frequent_feeds_are_fresh(self):
        now = CYCLE + timedelta(hours=8)
        payload = self.sources(now)
        self.assertFalse(deep_refresh_due(payload, now))
        for row in payload["sources"]:
            if row["id"] in DEEP_SOURCES:
                row["lastRun"]["completedAt"] = format_cycle_key(now - timedelta(hours=6))
        self.assertTrue(deep_refresh_due(payload, now))
        self.assertTrue(source_refresh_due(payload, now))

    def test_failed_source_completion_never_counts_as_fresh(self):
        now = CYCLE + timedelta(minutes=70)
        for replacement in [None, {"status": "FAILED"},
                            {"status": "RUNNING", "completedAt": None},
                            {"status": "SUCCEEDED", "completedAt": "invalid"}]:
            payload = self.sources(now)
            payload["sources"][0]["lastRun"] = replacement
            self.assertTrue(source_refresh_due(payload, now))

    def test_failed_status_reads_are_bounded_by_actual_collection_attempts(self):
        now = CYCLE + timedelta(minutes=70)
        def unavailable():
            raise RuntimeError("not accessible")
        for age, expected in [(0, False), (9, False), (10, True), (31, True)]:
            api = fake_api(started_at=format_cycle_key(now - timedelta(minutes=age)),
                           completed_at=None, conclusion="failure")
            due, _ = scheduler_is_due(api, "owner/repo", "securus-scheduler.yml", "main",
                cycle_start(now), now=now, source_get=unavailable)
            self.assertEqual(due, expected)
        due, _ = scheduler_is_due(fake_api(job_name="collect-and-scan", started_at=format_cycle_key(now)),
            "owner/repo", "securus-scheduler.yml", "main", cycle_start(now), now=now, source_get=unavailable)
        self.assertTrue(due, "a scan-only rerun cannot postpone recovery")

    def test_missing_cooldown_history_cannot_start_recursive_recovery_storm(self):
        now = CYCLE + timedelta(minutes=70)
        def unavailable(*args):
            raise RuntimeError("unavailable")
        dispatched = []
        due, reason = recover_cycle(unavailable, lambda *args, **kwargs: dispatched.append(args),
            "owner/repo", "securus-scheduler.yml", "main", cycle_start(now), now=now, source_get=unavailable)
        self.assertFalse(due)
        self.assertEqual(dispatched, [])
        self.assertIn("not confirmed healthy", reason)

    def test_delayed_or_replayed_trigger_promotes_to_current_cycle(self):
        now = CYCLE + timedelta(minutes=91)
        current = cycle_start(now)
        self.assertEqual(effective_cycle(CYCLE, now), current)
        self.assertEqual(effective_cycle(latest_slot(now, 7), now), current)
        self.assertEqual(effective_cycle(current, now), current)
        with self.assertRaises(ValueError):
            effective_cycle(current + timedelta(minutes=30), now)

    def test_live_freshness_drives_dispatch_and_retains_canonical_idempotency_key(self):
        now = CYCLE + timedelta(minutes=70)
        dispatched = []
        def request(path, **kwargs):
            dispatched.append((path, kwargs))
            return {}
        for age, expected in [(5, False), (45, True)]:
            due, _ = recover_cycle(fake_api(), request, "owner/repo", "securus-scheduler.yml", "main",
                effective_cycle(CYCLE, now), now=now, source_get=lambda: self.sources(now, age))
            self.assertEqual(due, expected)
        self.assertEqual(len(dispatched), 1)
        self.assertEqual(dispatched[0][1]["payload"]["inputs"], {
            "recovery": "true", "cycle_key": format_cycle_key(cycle_start(now)),
        })

    def test_public_status_reader_is_bounded_and_never_sends_github_credentials(self):
        from io import BytesIO
        response = BytesIO(b'{"sources":[]}')
        response.headers = {"Content-Type": "application/json"}
        with patch("scripts.check_scheduler_due.urlopen", return_value=response) as opened:
            self.assertEqual(read_public_source_status(), {"sources": []})
        request = opened.call_args.args[0]
        self.assertFalse(request.has_header("Authorization"))
        self.assertTrue(request.full_url.startswith("https://edgelab-sports."))
        for content, content_type in [(b"x" * 524289, "application/json"), (b"{}", "text/html"),
                                      (b"[]", "application/json")]:
            response = BytesIO(content)
            response.headers = {"Content-Type": content_type}
            with patch("scripts.check_scheduler_due.urlopen", return_value=response):
                with self.assertRaises(ValueError):
                    read_public_source_status()


if __name__ == "__main__":
    unittest.main()

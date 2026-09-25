from datetime import datetime, timedelta, timezone
from contextlib import redirect_stdout
from io import StringIO
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
    frequent_refresh_due,
    read_public_source_status,
    FREQUENT_SOURCES,
    DEEP_SOURCES,
    deep_refresh_due,
    collection_in_progress,
    abandoned_collections,
    gate_state,
    ABANDONED_COLLECTION_MINUTES,
    main,
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
            return {"workflow_runs": [{"id": 42, "head_branch": "main", "event": "schedule", "status": "completed"}]}
        return {"jobs": [{
            "name": job_name,
            "conclusion": conclusion,
            "started_at": started_at,
            "completed_at": completed_at,
        }]}
    return load


class SchedulerDueTests(unittest.TestCase):
    def run_cli(self, outcome, *, dispatch=True):
        argv = ['check_scheduler_due.py', '--repository', 'owner/repo']
        if dispatch:
            argv.append('--dispatch-if-due')
        output = StringIO()
        with patch('sys.argv', argv), patch('scripts.check_scheduler_due.GitHubApi'), \
                patch('scripts.check_scheduler_due.recover_cycle', return_value=outcome), \
                patch('scripts.check_scheduler_due.scheduler_is_due', return_value=outcome), \
                redirect_stdout(output):
            return main(), output.getvalue()

    def test_watchdog_cli_cannot_report_deferred_as_success(self):
        for reason in [
            'a frequent feed is due; storage maintenance is within the bounded 10-minute retry cooldown',
            'collection history unavailable; recovery deferred, not confirmed healthy',
            'live storage health unavailable; collection blocked',
        ]:
            code, output = self.run_cli((False, reason))
            self.assertEqual(code, 1)
            self.assertIn('Gate state: DEFERRED.', output)
            self.assertIn('::error::', output)
            self.assertNotIn('Dispatched the existing', output)

    def test_watchdog_cli_keeps_fresh_pending_and_accepted_dispatch_distinct(self):
        for outcome, state in [
            ((False, 'frequent feeds are under 30 minutes old'), 'FRESH'),
            ((False, 'runtime already queued or in progress'), 'PENDING'),
            ((True, 'a frequent feed is due for refresh'), 'DUE'),
        ]:
            code, output = self.run_cli(outcome)
            self.assertEqual(code, 0)
            self.assertIn(f'Gate state: {state}.', output)
            self.assertNotIn('::error::', output)

    def test_cadence_cli_preserves_its_existing_caller_owned_deferred_failure(self):
        code, output = self.run_cli((False, 'storage status unavailable'), dispatch=False)
        self.assertEqual(code, 0)
        self.assertIn('Gate state: DEFERRED.', output)
        self.assertIn('::warning::', output)

    def test_recovery_blocks_unknown_storage_and_pending_runtime(self):
        now = CYCLE + timedelta(hours=3)
        for capacity in [None, {},
                         {"capacityState": "NORMAL", "utilizationPercent": float('nan')}]:
            state = self.sources(now, 60)
            state["storage"]["capacity"] = capacity
            dispatched = []
            due, reason = recover_cycle(fake_api(), lambda *a, **kw: dispatched.append(kw),
                "owner/repo", "securus-scheduler.yml", "main", cycle_start(now), now=now,
                source_get=lambda: state)
            self.assertFalse(due)
            self.assertIn("storage", reason)
            self.assertEqual(dispatched, [])
        for status in ["queued", "in_progress", "waiting", "pending", "requested"]:
            def api(path):
                if "/runs?" in path:
                    return {"workflow_runs": [{"id": 42, "head_branch": "main", "event": "workflow_dispatch", "status": status}]}
                self.fail("pending runtime must block before job reads")
            due, reason = scheduler_is_due(api, "owner/repo", "securus-scheduler.yml", "main",
                cycle_start(now), now=now, source_get=lambda: self.sources(now, 60))
            self.assertFalse(due)
            self.assertIn("queued or in progress", reason)

    def test_critical_storage_only_dispatches_separate_maintenance_mode(self):
        now = CYCLE + timedelta(hours=3)
        state = self.sources(now, 60)
        state["storage"]["capacity"] = {"capacityState": "CRITICAL", "utilizationPercent": 85.2}
        calls = []
        due, _ = scheduler_is_due(fake_api(), "owner/repo", "securus-scheduler.yml", "main",
            cycle_start(now), now=now, source_get=lambda: state)
        self.assertFalse(due, "critical storage must never allow collection")
        due, _ = recover_cycle(fake_api(), lambda *a, **kw: calls.append(kw),
            "owner/repo", "securus-scheduler.yml", "main", cycle_start(now), now=now,
            source_get=lambda: state)
        self.assertTrue(due)
        self.assertEqual(calls[0]["payload"]["inputs"]["maintenance_only"], "true")
        self.assertEqual(calls[0]["payload"]["ref"], "main")

    def test_recent_maintenance_and_unfinished_collectors_block_repair_loops(self):
        now = CYCLE + timedelta(hours=3)
        state = self.sources(now, 60)
        state["storage"]["capacity"] = {"capacityState": "CRITICAL", "utilizationPercent": 85.2}
        calls = []
        api = fake_api(job_name="maintain-storage", started_at=(now-timedelta(minutes=2)).isoformat())
        due, _ = recover_cycle(api, lambda *a, **kw: calls.append(kw), "owner/repo",
            "securus-scheduler.yml", "main", cycle_start(now), now=now, source_get=lambda: state)
        self.assertFalse(due)
        state["sources"][0]["lastRun"] = {"status": "RUNNING", "startedAt": format_cycle_key(now - timedelta(minutes=2))}
        due, _ = recover_cycle(fake_api(), lambda *a, **kw: calls.append(kw), "owner/repo",
            "securus-scheduler.yml", "main", cycle_start(now), now=now, source_get=lambda: state)
        self.assertFalse(due)
        self.assertEqual(calls, [])

    def test_recent_independent_maintenance_blocks_all_recovery_modes(self):
        now = CYCLE + timedelta(hours=3)
        state = self.sources(now, 60)
        state["storage"]["assurance"] = {
            "lastIndependentMaintenanceAt": format_cycle_key(now - timedelta(minutes=8)),
        }
        calls = []
        due, reason = recover_cycle(fake_api(), lambda *a, **kw: calls.append(kw),
            "owner/repo", "securus-scheduler.yml", "main", cycle_start(now), now=now,
            source_get=lambda: state)
        self.assertFalse(due)
        self.assertIn("storage maintenance", reason)
        self.assertIn("cooldown", reason)
        self.assertEqual(calls, [])

        state["storage"]["assurance"]["lastIndependentMaintenanceAt"] = format_cycle_key(
            now - timedelta(minutes=12)
        )
        state["storage"]["capacity"]["archives"] = {"lastMaintenance": {
            "triggerName": "public-github-actions-verified",
            "completedAt": format_cycle_key(now),
        }}
        due, _ = recover_cycle(fake_api(), lambda *a, **kw: calls.append(kw),
            "owner/repo", "securus-scheduler.yml", "main", cycle_start(now), now=now,
            source_get=lambda: state)
        self.assertTrue(due, "the current run's own maintenance must not defer collection")
        self.assertEqual(len(calls), 1)
        calls.clear()

        state["storage"]["assurance"]["lastIndependentMaintenanceAt"] = format_cycle_key(
            now - timedelta(minutes=8)
        )
        state["storage"]["capacity"] = {
            "capacityState": "CRITICAL", "utilizationPercent": 85.2,
            "archives": {"lastMaintenance": {
                "startedAt": format_cycle_key(now - timedelta(minutes=9)),
            }},
        }
        due, _ = recover_cycle(fake_api(), lambda *a, **kw: calls.append(kw),
            "owner/repo", "securus-scheduler.yml", "main", cycle_start(now), now=now,
            source_get=lambda: state)
        self.assertFalse(due, "recent external maintenance must also block maintenance-only dispatch")
        self.assertEqual(calls, [])

        state["storage"]["assurance"]["lastIndependentMaintenanceAt"] = format_cycle_key(
            now - timedelta(minutes=10)
        )
        state["storage"]["capacity"] = {"capacityState": "NORMAL", "utilizationPercent": 50}
        due, _ = recover_cycle(fake_api(), lambda *a, **kw: calls.append(kw),
            "owner/repo", "securus-scheduler.yml", "main", cycle_start(now), now=now,
            source_get=lambda: state)
        self.assertTrue(due, "the bounded maintenance cooldown expires at ten minutes")
        self.assertEqual(len(calls), 1)

    def test_current_cadence_run_does_not_block_itself(self):
        now = CYCLE + timedelta(hours=3)
        state = self.sources(now, 60)
        state["storage"]["assurance"] = {
            "lastIndependentMaintenanceAt": format_cycle_key(now),
        }
        state["storage"]["capacity"]["archives"] = {"lastMaintenance": {
            "triggerName": "public-github-actions-verified",
            "completedAt": format_cycle_key(now),
        }}
        api = lambda path: {"workflow_runs": [{"id": 42, "head_branch": "main", "event": "schedule", "status": "in_progress"}]}
        due, _ = scheduler_is_due(api, "owner/repo", "securus-scheduler.yml", "main", cycle_start(now),
            now=now, source_get=lambda: state, exclude_run_id=42)
        self.assertTrue(due)

    def test_completed_ten_minute_cleanup_cannot_starve_due_feeds_with_normal_storage(self):
        for elapsed in (0, 10, 20):
            now = CYCLE + timedelta(hours=3, minutes=elapsed)
            state = self.sources(now, 60)
            state["storage"]["capacity"] = {
                "capacityState": "NORMAL", "maintenanceState": "NORMAL", "utilizationPercent": 34,
                "archives": {"lastMaintenance": {
                    "triggerName": "cloudflare-cron-10m", "status": "SUCCEEDED", "error": None,
                    "startedAt": format_cycle_key(now - timedelta(minutes=2)),
                    "completedAt": format_cycle_key(now - timedelta(minutes=1)),
                }},
            }
            calls = []
            due, reason = recover_cycle(fake_api(), lambda *a, **kw: calls.append(kw),
                "owner/repo", "securus-scheduler.yml", "main", cycle_start(now), now=now,
                source_get=lambda: state)
            self.assertTrue(due, reason)
            self.assertEqual(len(calls), 1)
            self.assertNotEqual(calls[0]["payload"]["inputs"].get("maintenance_only"), "true")
            for status in ("RUNNING", "FAILED", None):
                state["storage"]["capacity"]["archives"]["lastMaintenance"]["status"] = status
                due, _ = scheduler_is_due(fake_api(), "owner/repo", "securus-scheduler.yml", "main",
                    cycle_start(now), now=now, source_get=lambda: state)
                self.assertFalse(due, "unfinished, failed or unknown maintenance still honors cooldown")
            receipt = state["storage"]["capacity"]["archives"]["lastMaintenance"]
            receipt["status"] = "SUCCEEDED"
            state["storage"]["capacity"].update(capacityState="WARNING", utilizationPercent=78)
            due, _ = scheduler_is_due(fake_api(), "owner/repo", "securus-scheduler.yml", "main",
                cycle_start(now), now=now, source_get=lambda: state)
            self.assertFalse(due, "storage under pressure keeps its maintenance cooldown")

    def test_external_maintenance_receipt_still_blocks_current_cadence_run(self):
        now = CYCLE + timedelta(hours=3)
        state = self.sources(now, 60)
        state["storage"]["assurance"] = {
            "lastIndependentMaintenanceAt": format_cycle_key(now - timedelta(minutes=2)),
        }
        state["storage"]["capacity"]["archives"] = {"lastMaintenance": {
            "triggerName": "cloudflare-cron-10m",
            "completedAt": format_cycle_key(now - timedelta(minutes=2)),
        }}
        api = lambda path: {"workflow_runs": [{"id": 42, "head_branch": "main", "event": "schedule", "status": "in_progress"}]}
        due, reason = scheduler_is_due(api, "owner/repo", "securus-scheduler.yml", "main", cycle_start(now),
            now=now, source_get=lambda: state, exclude_run_id=42)
        self.assertFalse(due)
        self.assertIn("storage maintenance", reason)

    def test_current_cadence_run_ignores_only_queued_concurrency_followers(self):
        now = CYCLE + timedelta(hours=3)
        state = self.sources(now, 60)

        def history(status):
            def load(path):
                if "/runs?" in path:
                    return {"workflow_runs": [
                        {"id": 42, "head_branch": "main", "event": "schedule", "status": "in_progress"},
                        {"id": 43, "head_branch": "main", "event": "workflow_dispatch", "status": status},
                    ]}
                self.fail("queued followers have no job history yet")
            return load

        for status in ("queued", "pending", "requested"):
            with self.subTest(status=status):
                due, reason = scheduler_is_due(
                    history(status), "owner/repo", "securus-scheduler.yml", "main",
                    cycle_start(now), now=now, source_get=lambda: state,
                    exclude_run_id=42,
                )
                self.assertTrue(due)
                self.assertIn("frequent feed", reason)

        for status in ("in_progress", "waiting"):
            with self.subTest(status=status):
                due, reason = scheduler_is_due(
                    history(status), "owner/repo", "securus-scheduler.yml", "main",
                    cycle_start(now), now=now, source_get=lambda: state,
                    exclude_run_id=42,
                )
                self.assertFalse(due)
                self.assertIn("queued or in progress", reason)

        # The watchdog has no current run to exclude and must remain blocked by
        # every queued follower before it dispatches another recovery.
        due, reason = scheduler_is_due(
            history("queued"), "owner/repo", "securus-scheduler.yml", "main",
            cycle_start(now), now=now, source_get=lambda: state,
        )
        self.assertFalse(due)
        self.assertIn("queued or in progress", reason)

    def test_warning_storage_gets_maintenance_even_with_fresh_feeds(self):
        now = CYCLE + timedelta(hours=3)
        state = self.sources(now, 0)
        state["storage"]["capacity"] = {"capacityState": "WARNING", "utilizationPercent": 83.8}
        calls = []
        due, reason = recover_cycle(fake_api(), lambda *a, **kw: calls.append(kw),
            "owner/repo", "securus-scheduler.yml", "main", cycle_start(now), now=now,
            source_get=lambda: state)
        self.assertTrue(due)
        self.assertIn("storage warning", reason)
        self.assertEqual(calls[0]["payload"]["inputs"]["maintenance_only"], "true")
        self.assertEqual(len(calls), 1)

    def test_warning_cleanup_never_displaces_due_collection(self):
        now = CYCLE + timedelta(hours=3)
        state = self.sources(now, 45)
        state["storage"]["capacity"] = {"capacityState": "WARNING", "utilizationPercent": 83.8}
        calls = []
        due, _ = recover_cycle(fake_api(), lambda *a, **kw: calls.append(kw),
            "owner/repo", "securus-scheduler.yml", "main", cycle_start(now), now=now,
            source_get=lambda: state)
        self.assertTrue(due)
        self.assertNotIn("maintenance_only", calls[0]["payload"]["inputs"])

    def test_warning_cleanup_retains_cooldown_and_known_idle_requirements(self):
        now = CYCLE + timedelta(hours=3)
        state = self.sources(now, 5)
        state["storage"]["capacity"] = {"capacityState": "WARNING", "utilizationPercent": 83.8}
        def pending(path):
            return {"workflow_runs": [{"id": 42, "head_branch": "main", "event": "schedule", "status": "in_progress"}]}
        recent = fake_api(job_name="maintain-storage", started_at=(now-timedelta(minutes=2)).isoformat())
        for api in [recent, pending, lambda _: {}]:
            calls = []
            due, _ = recover_cycle(api, lambda *a, **kw: calls.append(kw),
                "owner/repo", "securus-scheduler.yml", "main", cycle_start(now), now=now,
                source_get=lambda: state)
            self.assertFalse(due)
            self.assertEqual(calls, [])
        state["sources"][0]["lastRun"] = {"status": "RUNNING", "startedAt": format_cycle_key(now - timedelta(minutes=2))}
        calls = []
        due, _ = recover_cycle(fake_api(), lambda *a, **kw: calls.append(kw),
            "owner/repo", "securus-scheduler.yml", "main", cycle_start(now), now=now,
            source_get=lambda: state)
        self.assertFalse(due)
        self.assertEqual(calls, [])

    def test_abandoned_running_receipts_cannot_defer_recovery_forever(self):
        # 2026-09-13: one RUNNING receipt deferred every scheduled and watchdog
        # check for ~15 hours. A dead journal must be bounded, never a live worker.
        now = CYCLE + timedelta(hours=3)
        for started in [format_cycle_key(now - timedelta(minutes=ABANDONED_COLLECTION_MINUTES)),
                        format_cycle_key(now - timedelta(hours=15)), None, "", "not-a-time",
                        format_cycle_key(now + timedelta(minutes=5))]:
            with self.subTest(started=started):
                state = self.sources(now, 5)
                state["sources"][0]["lastRun"] = {"status": "RUNNING", "startedAt": started}
                self.assertFalse(collection_in_progress(state, now))
                self.assertEqual(abandoned_collections(state, now), [state["sources"][0]["id"]])
                calls = []
                due, reason = recover_cycle(fake_api(), lambda *a, **kw: calls.append(kw),
                    "owner/repo", "securus-scheduler.yml", "main", cycle_start(now), now=now,
                    source_get=lambda: state)
                self.assertTrue(due)
                self.assertIn("abandoned collector receipt", reason)
                self.assertIn(state["sources"][0]["id"], reason)
                self.assertEqual(len(calls), 1)
                self.assertNotIn("maintenance_only", calls[0]["payload"]["inputs"])
        marker = self.sources(now, 0)
        marker["sources"].append({"id": "site-cron-recovery", "lastRun": {"status": "RUNNING",
            "startedAt": format_cycle_key(now - timedelta(minutes=1))}})
        self.assertFalse(collection_in_progress(marker, now),
            "a Site bookkeeping row is not collection evidence and cannot defer the gate")
        self.assertEqual(abandoned_collections(marker, now), [])
        due, reason = scheduler_is_due(fake_api(), "owner/repo", "securus-scheduler.yml", "main",
            cycle_start(now), now=now, source_get=lambda: marker)
        self.assertFalse(due)
        self.assertIn("frequent feeds are under", reason)
        live = self.sources(now, 5)
        live["sources"][0]["lastRun"] = {"status": "RUNNING",
            "startedAt": format_cycle_key(now - timedelta(minutes=ABANDONED_COLLECTION_MINUTES - 1))}
        self.assertTrue(collection_in_progress(live, now))
        self.assertEqual(abandoned_collections(live, now), [])
        due, reason = scheduler_is_due(fake_api(), "owner/repo", "securus-scheduler.yml", "main",
            cycle_start(now), now=now, source_get=lambda: live)
        self.assertFalse(due)
        self.assertIn("unfinished", reason)
        stale = self.sources(now, 5)
        stale["sources"][0]["lastRun"] = {"status": "RUNNING", "startedAt": format_cycle_key(now - timedelta(hours=1))}
        due, reason = scheduler_is_due(fake_api(started_at=format_cycle_key(now - timedelta(minutes=2)),
            completed_at=None, conclusion="failure"), "owner/repo", "securus-scheduler.yml", "main",
            cycle_start(now), now=now, source_get=lambda: stale)
        self.assertFalse(due, "an abandoned receipt still honors the bounded retry cooldown")
        self.assertIn("cooldown", reason)

    def test_gate_state_never_reports_a_deferred_stale_cycle_as_fresh(self):
        self.assertEqual(gate_state(True, "a frequent feed is missing"), "DUE")
        self.assertEqual(gate_state(False, "frequent feeds are under 30 minutes old and deep feeds are within their six-hour cadence"), "FRESH")
        self.assertEqual(gate_state(False, "cycle 2026-09-03T05:07:00Z completed at 2026-09-03T05:12:00Z"), "FRESH")
        self.assertEqual(gate_state(False, "runtime already queued or in progress"), "PENDING")
        for reason in ["a runtime source collection is unfinished; recovery deferred",
                       "live source timestamps and storage could not be verified; recovery deferred, not confirmed healthy",
                       "collection history unavailable; recovery deferred to the next check, not confirmed healthy",
                       "a frequent feed is missing, failed, or due for its 30-minute refresh; collection attempt is within the bounded 10-minute retry cooldown; readiness remains fail-closed",
                       "storage critical; verified maintenance is required before collection",
                       "GitHub status could not be verified; recovery deferred, not confirmed healthy"]:
            self.assertEqual(gate_state(False, reason), "DEFERRED", reason)

    def test_malformed_history_is_not_an_empty_successful_history(self):
        now = CYCLE + timedelta(hours=3)
        for history in [{}, {"workflow_runs": None}, {"workflow_runs": [{"id": 42}]}]:
            due, reason = scheduler_is_due(lambda path: history, "owner/repo", "securus-scheduler.yml", "main",
                cycle_start(now), now=now, source_get=lambda: self.sources(now, 60))
            self.assertFalse(due)
            self.assertIn("history unavailable", reason)

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
        self.assertEqual(due, False)
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

    def test_explicit_cron_slot_tolerates_observed_early_delivery(self):
        now = datetime(2026, 9, 25, 14, 6, 38, tzinfo=timezone.utc)
        slot = latest_slot(now, 7)
        self.assertEqual(slot, datetime(2026, 9, 25, 14, 7, tzinfo=timezone.utc))
        self.assertEqual(effective_cycle(slot, now), slot)

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
        return {"lastPaperScan": {"runId": "completed-test-scan", "mode": "PAPER_ONLY", "completedAt": format_cycle_key(now)},
                "storage": {"capacity": {"capacityState": "NORMAL", "utilizationPercent": 50}}, "sources": [{"id": source, "lastRun": {
            "status": "SUCCEEDED", "completedAt": format_cycle_key(now - timedelta(minutes=age)),
        }} for source in FREQUENT_SOURCES | DEEP_SOURCES]}

    def test_fresh_feeds_cannot_hide_a_missing_or_stalled_paper_scan(self):
        now = CYCLE + timedelta(hours=3)
        for scan in [None, {}, {"runId": "old", "mode": "PAPER_ONLY", "completedAt": format_cycle_key(now - timedelta(minutes=61))}]:
            state = self.sources(now, 0)
            state["lastPaperScan"] = scan
            calls = []
            due, reason = recover_cycle(fake_api(), lambda *a, **kw: calls.append(kw),
                "owner/repo", "securus-scheduler.yml", "main", cycle_start(now), now=now,
                source_get=lambda: state)
            self.assertTrue(due)
            self.assertIn("completed paper scan", reason)
            self.assertEqual(len(calls), 1)
            self.assertNotIn("maintenance_only", calls[0]["payload"]["inputs"])

    def test_missing_scan_recovery_retains_cooldown(self):
        now = CYCLE + timedelta(hours=3)
        state = self.sources(now, 5)
        state["lastPaperScan"] = None
        calls = []
        due, reason = recover_cycle(fake_api(started_at=format_cycle_key(now - timedelta(minutes=2))),
            lambda *a, **kw: calls.append(kw), "owner/repo", "securus-scheduler.yml", "main",
            cycle_start(now), now=now, source_get=lambda: state)
        self.assertFalse(due)
        self.assertIn("cooldown", reason)
        self.assertEqual(calls, [])

    def test_actual_sources_not_a_green_workflow_determine_refresh(self):
        now = CYCLE + timedelta(minutes=70)
        boundary = cycle_start(now)
        for age, expected in [(0, False), (9, False), (10, False), (29, True),
                              (30, True), (46, True), (-1, True)]:
            with self.subTest(age=age):
                due, _ = scheduler_is_due(fake_api(), "owner/repo", "securus-scheduler.yml", "main",
                    boundary, now=now, source_get=lambda: self.sources(now, age))
                self.assertEqual(due, expected)
        payload = self.sources(now)
        payload["paperBetReadiness"] = {"status": "NO_BET", "readySports": []}
        self.assertFalse(source_refresh_due(payload, now),
            "a betting-policy abstention must never cause a collection retry storm")

    def test_runtime_age_cannot_skip_the_next_canonical_cycle(self):
        now = datetime(2026, 9, 25, 14, 7, 10, tzinfo=timezone.utc)
        payload = self.sources(now, 28)
        due, reason = scheduler_is_due(
            fake_api(started_at="2026-09-25T13:37:10Z", completed_at="2026-09-25T13:41:00Z"),
            "owner/repo", "securus-scheduler.yml", "main", cycle_start(now),
            now=now, source_get=lambda: payload,
        )
        self.assertTrue(due)
        self.assertIn("canonical cycle", reason)

    def test_missing_failed_and_incomplete_source_records_need_refresh(self):
        now = CYCLE + timedelta(minutes=70)
        for payload in [{}, {"sources": None}, {"sources": []},
                        {"sources": [{"id": source} for source in FREQUENT_SOURCES]}]:
            self.assertTrue(source_refresh_due(payload, now))

    def test_deep_sources_cannot_starve_when_frequent_feeds_are_fresh(self):
        now = CYCLE + timedelta(hours=8)
        payload = self.sources(now)
        self.assertFalse(deep_refresh_due(payload, now))
        self.assertFalse(frequent_refresh_due(payload, now))
        for row in payload["sources"]:
            if row["id"] in DEEP_SOURCES:
                row["lastRun"]["completedAt"] = format_cycle_key(now - timedelta(hours=6))
        self.assertTrue(deep_refresh_due(payload, now))
        self.assertFalse(frequent_refresh_due(payload, now),
            "a deep-only cycle must not duplicate fresh high-volume feeds")
        self.assertTrue(source_refresh_due(payload, now))

    def test_frequent_due_is_reported_independently_from_deep_cadence(self):
        now = CYCLE + timedelta(hours=8)
        payload = self.sources(now)
        for row in payload["sources"]:
            if row["id"] in FREQUENT_SOURCES:
                row["lastRun"]["completedAt"] = format_cycle_key(now - timedelta(minutes=30))
        self.assertTrue(frequent_refresh_due(payload, now))
        self.assertFalse(deep_refresh_due(payload, now))

    def test_failed_source_completion_never_counts_as_fresh(self):
        now = CYCLE + timedelta(minutes=70)
        for replacement in [None, {"status": "FAILED"},
                            {"status": "RUNNING", "completedAt": None},
                            {"status": "SUCCEEDED", "completedAt": "invalid"}]:
            payload = self.sources(now)
            payload["sources"][0]["lastRun"] = replacement
            self.assertTrue(source_refresh_due(payload, now))

    def test_cooldown_preserves_the_diagnostic_cause(self):
        now = CYCLE + timedelta(minutes=70)
        api = fake_api(started_at=format_cycle_key(now), completed_at=None, conclusion="failure")
        deep = self.sources(now)
        for row in deep["sources"]:
            if row["id"] in DEEP_SOURCES:
                row["lastRun"]["completedAt"] = format_cycle_key(now - timedelta(hours=6))
        def unavailable():
            raise RuntimeError("response body must not be logged")
        for source_get, cause in [
            (lambda: self.sources(now, 31), "frequent feed"),
            (lambda: deep, "deep-stat feed"),
        ]:
            due, reason = scheduler_is_due(api, "owner/repo", "securus-scheduler.yml", "main",
                cycle_start(now), now=now, source_get=source_get)
            self.assertFalse(due)
            self.assertIn(cause, reason)
            self.assertIn("cooldown", reason)
            self.assertNotIn("response body", reason)

    def test_failed_status_reads_are_bounded_by_actual_collection_attempts(self):
        now = CYCLE + timedelta(minutes=70)
        def unavailable():
            raise RuntimeError("not accessible")
        for age, expected in [(0, False), (9, False), (10, False), (31, False)]:
            api = fake_api(started_at=format_cycle_key(now - timedelta(minutes=age)),
                           completed_at=None, conclusion="failure")
            due, _ = scheduler_is_due(api, "owner/repo", "securus-scheduler.yml", "main",
                cycle_start(now), now=now, source_get=unavailable)
            self.assertEqual(due, expected)
        due, _ = scheduler_is_due(fake_api(job_name="collect-and-scan", started_at=format_cycle_key(now)),
            "owner/repo", "securus-scheduler.yml", "main", cycle_start(now), now=now, source_get=unavailable)
        self.assertFalse(due, "unavailable storage must block recovery even after a scan-only rerun")

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

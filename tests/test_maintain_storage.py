import unittest
from scripts.maintain_storage import maintain


def capacity(percent):
    return dict(capacityState="CRITICAL" if percent >= 85 else "WARNING",
                utilizationPercent=percent, actualBytes=percent * 100, safetyCapacityBytes=10000)


class MaintenanceTests(unittest.TestCase):
    def run_fake(self, percents, statuses=(), **kwargs):
        calls, running = [], list(statuses)
        remaining = iter(percents)
        def api(path, payload=None):
            calls.append((path, payload))
            if path == "/api/data-sources":
                return {"storage": {"capacity": capacity(85.2)}}
            if payload is not None:
                return {"accepted": True, "runId": 42, "after": capacity(10),
                        "statusUrl": "https://untrusted.invalid/steal-token"}
            self.assertEqual(path, "/api/storage-maintenance-status?runId=42")
            status = running.pop(0) if running else "SUCCEEDED"
            return {"runId": 42, "status": status, "completedAt": "2026-09-11T17:00:00Z",
                    "capacity": capacity(next(remaining))}
        result = maintain(api, sleep=lambda _: None, clock=lambda: 0, **kwargs)
        return result, calls

    def test_waits_for_journal_and_measured_headroom_not_post_after(self):
        result, calls = self.run_fake([85.2, 84.8, 83.4], ["RUNNING"])
        self.assertEqual(result["capacity"]["utilizationPercent"], 83.4)
        self.assertEqual(sum(body is not None for _, body in calls), 2)

    def test_does_not_collect_when_cleanup_fails_or_headroom_is_insufficient(self):
        with self.assertRaisesRegex(RuntimeError, "failed"):
            self.run_fake([85.2], ["FAILED"])
        with self.assertRaisesRegex(RuntimeError, "did not reach"):
            self.run_fake([84.8], max_passes=1)

    def test_stagnant_cleanup_stops_before_exhausting_budget(self):
        with self.assertRaisesRegex(RuntimeError, "no capacity progress"):
            self.run_fake([85.2, 85.2, 85.2])

    def test_unknown_storage_blocks_even_maintenance(self):
        with self.assertRaisesRegex(RuntimeError, "unavailable"):
            maintain(lambda _: {}, clock=lambda: 0)

    def test_unfinished_journal_remains_pending(self):
        with self.assertRaisesRegex(RuntimeError, "pending"):
            self.run_fake([85.2] * 24, ["RUNNING"] * 24)


if __name__ == "__main__":
    unittest.main()

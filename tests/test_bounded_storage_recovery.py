import copy
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from scripts.check_scheduler_due import recover_cycle, storage_lockout_recovery_allowed

NOW = datetime.fromisoformat("2026-11-20T13:34:00+00:00")
ATTEMPT = NOW - timedelta(minutes=13)
STATUS = {"sources": [{"id": "propline", "lastRun": {"status": "FAILED"}}],
          "storage": {"capacity": {"capacityState": "WARNING", "maintenanceState": "WARNING",
                                   "utilizationPercent": 84.81}}}
INCIDENT = {"time": "2026-11-20 13:24:35", "route": "/api/collect",
            "status": 507, "code": "INSUFFICIENT_STORAGE"}


class StorageRecovery(unittest.TestCase):
    def test_only_exact_fresh_storage_failure_qualifies(self):
        payload = {"incidents": [INCIDENT]}
        self.assertTrue(storage_lockout_recovery_allowed(STATUS, payload, ATTEMPT, NOW))
        for changes in ({"status": 409}, {"route": "/api/ready"}, {"code": "DATABASE_UNAVAILABLE"},
                        {"time": "2026-11-20 13:34:10"}, {"time": "2026-11-20 13:01:00"}):
            self.assertFalse(storage_lockout_recovery_allowed(
                STATUS, {"incidents": [INCIDENT | changes]}, ATTEMPT, NOW))
        self.assertFalse(storage_lockout_recovery_allowed(
            STATUS, payload, NOW - timedelta(minutes=9), NOW))
        self.assertFalse(storage_lockout_recovery_allowed(
            STATUS, payload, datetime.fromisoformat("2026-11-20T13:25:00+00:00"),
            NOW + timedelta(minutes=10)))

    def test_active_propline_and_nonwarning_never_override(self):
        payload = {"incidents": [INCIDENT]}
        status = copy.deepcopy(STATUS)
        status["sources"][0]["lastRun"] = {"status": "RUNNING", "startedAt": NOW.isoformat()}
        self.assertFalse(storage_lockout_recovery_allowed(status, payload, ATTEMPT, NOW))
        status = copy.deepcopy(STATUS)
        status["storage"]["capacity"]["utilizationPercent"] = 83.49
        self.assertFalse(storage_lockout_recovery_allowed(status, payload, ATTEMPT, NOW))

    @patch("scripts.check_scheduler_due.collection_timestamps", return_value=(ATTEMPT, None))
    @patch("scripts.check_scheduler_due.scheduler_is_due", return_value=(True, "stale"))
    def test_dispatch_preserves_existing_origin_and_limits_scope(self, _due, _times):
        requests = []
        due, reason = recover_cycle(
            lambda *_: {}, lambda path, **kw: requests.append((path, kw)),
            "owner/repo", "securus-scheduler.yml", "main", NOW.replace(minute=7),
            now=NOW, source_get=lambda: STATUS, incidents_get=lambda: {"incidents": [INCIDENT]})
        self.assertTrue(due)
        self.assertIn("source recovery unverified", reason)
        self.assertEqual(requests[0][1]["payload"]["inputs"],
                         {"maintenance_only": "true", "recovery": "true",
                          "cycle_key": "2026-11-20T13:07:00Z"})


if __name__ == "__main__":
    unittest.main()

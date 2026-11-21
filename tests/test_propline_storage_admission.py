import unittest
from datetime import datetime

from scripts.verify_freshness import propline_storage_admission_failures


class StorageAdmissionTest(unittest.TestCase):
    def test_only_recent_consistent_noncritical_measured_size(self):
        cap = dict(sizeSource="D1_META", capacityState="WARNING", maintenanceState="WARNING",
                   actualBytes=84, safetyCapacityBytes=100, utilizationPercent=84.0,
                   maintenanceMeasuredAt="2026-11-21T13:07:00Z")
        now = datetime.fromisoformat("2026-11-21T13:12:00+00:00")
        def failures(**override):
            return propline_storage_admission_failures({"storage": {"capacity": cap | override}}, now)
        self.assertEqual(failures(), [])
        for override in [
            {"actualBytes": 8401, "safetyCapacityBytes": 10000},
            {"maintenanceState": "CRITICAL"}, {"capacityState": "CRITICAL"},
            {"maintenanceMeasuredAt": "2026-11-21T13:06:59.999Z"},
            {"maintenanceMeasuredAt": "2026-11-21T13:12:01Z"},
            {"sizeSource": "ROW_ESTIMATE"}, {"actualBytes": 83},
            {"utilizationPercent": float("nan")}, {"utilizationPercent": True},
        ]:
            with self.subTest(override=override):
                self.assertTrue(failures(**override))


if __name__ == "__main__":
    unittest.main()

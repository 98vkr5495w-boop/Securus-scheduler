from datetime import datetime
from unittest import TestCase
from unittest.mock import Mock

from scripts.recover_propline_receipt import verify_or_recover
from scripts.verify_freshness import propline_storage_admission_failures

START = "2026-11-25T10:26:13.208Z"
END = "2026-11-25T10:26:35.427Z"
VERIFY = "2026-11-25T10:26:36.570Z"
SLOT = "2026-11-25T10:07:00Z"
LIMIT = 524288000


def snap(used, measured="2026-11-25T10:26:44.507Z", **receipt):
    state = "CRITICAL" if used * 100 >= LIMIT * 85 else "WARNING"
    return {"storage": {"capacity": {
        "capacityState": state, "maintenanceState": state, "sizeSource": "D1_META",
        "utilizationPercent": round(100 * used / LIMIT, 2), "actualBytes": used,
        "safetyCapacityBytes": LIMIT, "maintenanceMeasuredAt": measured,
    }}, "sources": [{"id": "propline", "lastRun": {
        "startedAt": START, "completedAt": END, "status": "SUCCEEDED",
        "recordsWritten": 16423, **receipt,
    }}]}


class PostQuoteRecovery(TestCase):
    def replay(self, after, *, before=None, recovered_at="2026-11-25T10:28:01Z", cycle=SLOT):
        t = [datetime.fromisoformat(VERIFY)]
        api = Mock(side_effect=[snap(448876544) if before is None else before, after])
        clean = Mock(side_effect=lambda: t.__setitem__(0, datetime.fromisoformat(recovered_at)))
        result = verify_or_recover(api, clean, start=START, end=END, cycle=cycle, clock=lambda: t[0])
        return result, api, clean

    def test_2071_fixture_and_safe_counterfactual(self):
        before = snap(int(LIMIT * .8391), "2026-11-25T10:24:33Z")  # byte figure illustrative
        self.assertEqual(propline_storage_admission_failures(before, datetime.fromisoformat(START)), [])
        result, api, clean = self.replay(snap(439500800, "2026-11-25T10:28:00Z"))
        self.assertEqual(result, [])
        self.assertEqual(api.call_count, 2)
        clean.assert_called_once_with()

    def test_no_recovery_after_mismatch_or_without_canonical_slot(self):
        for kw in ({"before": snap(448876544, completedAt="2026-11-25T10:26:34Z")}, {"cycle":""}):
            result, api, clean = self.replay(None, **kw)
            self.assertTrue(result)
            self.assertEqual(api.call_count, 1)
            clean.assert_not_called()

    def test_cleanup_does_not_admit_changed_or_still_critical_receipt(self):
        for after in (snap(439500800, recordsWritten=16424), snap(448876544), snap(442000000),
                      snap(439500800, startedAt="2026-11-25T10:26:14Z"),
                      snap(439500800, "2026-11-25T10:26:00Z")):
            result, _, clean = self.replay(after)
            self.assertTrue(result)
            clean.assert_called_once_with()

    def test_cleanup_cannot_extend_freshness(self):
        result, _, clean = self.replay(snap(439500800, "2026-11-25T10:31:37Z"),
                                      recovered_at="2026-11-25T10:31:38Z")
        self.assertIn("outside five-minute window", result[0])
        clean.assert_called_once_with()

    def test_recovery_cannot_start_without_same_slot_budget(self):
        late = datetime.fromisoformat("2026-11-25T10:36:45Z")
        start = "2026-11-25T10:36:22Z"
        end = "2026-11-25T10:36:43Z"
        api = Mock(return_value=snap(448876544, startedAt=start, completedAt=end))
        clean = Mock()
        self.assertIn("insufficient time", verify_or_recover(
            api, clean, start=start, end=end, cycle=SLOT, clock=lambda: late)[-1])
        clean.assert_not_called()

    def test_safe_reread_avoids_cleanup(self):
        result, api, clean = self.replay(None, before=snap(439500800, "2026-11-25T10:26:36Z"))
        self.assertEqual(result, [])
        self.assertEqual(api.call_count, 1)
        clean.assert_not_called()

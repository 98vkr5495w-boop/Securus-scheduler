import copy
import unittest
from datetime import datetime

from scripts.run_crypto_paper import validate_completed_scan
from scripts.verify_freshness import propline_receipt_failures

SLOT = "2026-11-20T13:07:00Z"
START = "2026-11-20T13:13:12.000Z"
END = "2026-11-20T13:13:20.000Z"


class CurrentFanaticsContract(unittest.TestCase):
    def test_exact_paper_lane_legacy_preservation_and_no_money(self):
        result = dict(accepted=True, mode="PAPER_ONLY", analyst="SECURUS",
                      paperBettor="CRYPTO", venue="FANATICS", realMoneyExecution=False,
                      legacySettlementPreserved=True, decisions=[], errors=[])
        self.assertEqual(validate_completed_scan(result), [])
        for key, value in {"venue": "KALSHI", "legacySettlementPreserved": 1,
                           "realMoneyExecution": 0, "mode": "LIVE"}.items():
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                validate_completed_scan({**result, key: value})
        del result["legacySettlementPreserved"]
        with self.assertRaises(RuntimeError):
            validate_completed_scan(result)

    def test_required_fresh_durable_receipt_matches_this_slot(self):
        status = {"sources": [{"id": "propline", "lastRun": {
            "status": "SUCCEEDED", "recordsWritten": 150,
            "startedAt": START, "completedAt": END,
        }}], "storage": {"capacity": {"capacityState": "WARNING",
                                       "utilizationPercent": 83.7}}}
        kwargs = dict(expected_start=START, expected_end=END, cycle_key=SLOT,
                      now=datetime.fromisoformat("2026-11-20T13:18:20+00:00"))
        self.assertEqual(propline_receipt_failures(status, **kwargs), [])
        for override in [
            {"expected_start": ""}, {"cycle_key": "2026-11-20T12:37:00Z"},
            {"cycle_key": "2026-11-20T13:08:00Z"},
            {"cycle_key": "2026-11-20T13:37:00Z"},
            {"now": datetime.fromisoformat("2026-11-20T13:18:20.001+00:00")},
            {"now": datetime.fromisoformat("2026-11-20T13:13:19+00:00")},
        ]:
            with self.subTest(override=override):
                self.assertTrue(propline_receipt_failures(status, **(kwargs | override)))
        for override in [{"status": "FAILED"}, {"status": "RUNNING"},
                         {"recordsWritten": True}, {"recordsWritten": 0},
                         {"startedAt": "2026-11-20T13:13:11Z"}]:
            with self.subTest(override=override):
                payload = copy.deepcopy(status)
                payload["sources"][0]["lastRun"].update(override)
                self.assertTrue(propline_receipt_failures(payload, **kwargs))
        payload = copy.deepcopy(status)
        payload["storage"]["capacity"]["maintenanceState"] = "CRITICAL"
        self.assertTrue(propline_receipt_failures(payload, **kwargs))
        self.assertTrue(propline_receipt_failures({"sources": []}, **kwargs))


if __name__ == "__main__":
    unittest.main()

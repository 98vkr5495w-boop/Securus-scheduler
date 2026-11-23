import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from collect_deep_after_cleanup import run, measurement, active_slot


class DeepGuardTest(unittest.TestCase):
    SLOT = "2026-11-23T03:07:00Z"
    SCAN = "2026-11-23T03:26:18.167Z"

    def setUp(self):
        self.time = datetime(2026, 11, 23, 3, 26, 20, tzinfo=timezone.utc)
        self.calls = []

    def payload(self, percent=83.9, *, utc=None, source="D1_META", health="WARNING", fresh=False):
        now = utc or self.time
        return {
            "storage": {"capacity": {"capacityState": health, "maintenanceState": health,
                "actualBytes": round(100*percent), "safetyCapacityBytes": 10000,
                "utilizationPercent": percent, "sizeSource": source}},
            "paperBetReadiness": {"checkedAt": now.isoformat().replace("+00:00","Z")},
            "sources": [{"id": "nflverse", "lastRun": {"status":"SUCCEEDED",
                 "completedAt": now.isoformat().replace("+00:00","Z")}}] if fresh else [],
        }

    def clock(self):
        return self.time

    def sleep(self, seconds):
        self.time += timedelta(seconds=seconds)

    def call(self, argv, bound):
        self.calls.append((argv[-1], bound))
        self.sleep(2)
        return 0

    def guard(self, read=None, call=None, slot=None, scan=None, manual=False):
        return run(self.SLOT if slot is None else slot, self.SCAN if scan is None else scan,
                   manual=manual, api=read or self.payload, now=self.clock,
                   call=call or self.call, sleep=self.sleep)

    def test_two_independent_probes_and_no_maintenance_when_admitted(self):
        reads=[]
        def read():
            reads.append(self.clock())
            return self.payload()
        self.assertEqual(self.guard(read),0)
        self.assertEqual([a for a,b in self.calls],
                         ["mode=daily&source=nflverse", "mode=daily&source=baseball-savant"])
        self.assertEqual([b for a,b in self.calls],[122,122])
        self.assertLess(reads[0], reads[1])

    def test_live_like_capacity_gets_one_bounded_drain_after_scan(self):
        payloads=iter([84.42,83.5,83.95])
        self.assertEqual(self.guard(lambda:self.payload(next(payloads))),0)
        self.assertEqual([a for a,b in self.calls],
                         ["3","mode=daily&source=nflverse", "mode=daily&source=baseball-savant"])
        self.assertEqual(self.calls[0][1],130)

    def test_cleanup_does_not_allow_unsafe_capacity_or_recur(self):
        self.assertEqual(self.guard(lambda:self.payload(84.42)),1)
        self.assertEqual(self.calls,[("3",130)])

    def test_old_generation_after_first_post_never_authorizes_second(self):
        first_time=self.clock()
        self.assertEqual(self.guard(lambda:self.payload(utc=first_time)),1)
        self.assertEqual(self.calls,[("mode=daily&source=nflverse",122)])

    def test_ambiguous_daily_result_never_retried_and_no_second_write(self):
        def network_error(argv,bound):
            self.call(argv,bound)
            raise subprocess.TimeoutExpired(argv,bound)
        self.assertEqual(self.guard(call=network_error),1)
        self.assertEqual(self.calls,[("mode=daily&source=nflverse",122)])

    def test_fresh_one_does_not_make_the_other_source_fresh(self):
        self.assertEqual(self.guard(lambda:self.payload(fresh=True)),0)
        self.assertEqual(self.calls,[("mode=daily&source=baseball-savant",122)])

    def test_invalid_or_unverified_current_slot_never_probes(self):
        for slot, scan in [("bad",self.SCAN),(self.SLOT,""),("2026-11-23T02:37:00Z",self.SCAN)]:
            self.assertEqual(self.guard(lambda:self.fail("unexpected probe"),slot=slot,scan=scan),1)
        self.assertEqual(self.calls,[])

    def test_late_slot_reserves_hard_timeout_and_uses_strict_boundary(self):
        self.time=datetime(2026,11,23,3,34,55,tzinfo=timezone.utc)
        self.assertEqual(self.guard(lambda:self.fail("unexpected probe")),1)
        self.assertEqual(self.calls,[])

    def test_dashboard_rounding_does_not_relax_exact_cap(self):
        p=self.payload(84)
        capacity=p["storage"]["capacity"]
        capacity.update(actualBytes=84001,safetyCapacityBytes=100000)
        self.assertEqual(measurement(p,self.clock(),self.clock()),"NEEDS_CLEANUP")

    def test_estimated_size_is_not_admitted(self):
        self.assertEqual(self.guard(lambda:self.payload(source="ESTIMATE")),1)
        self.assertEqual(self.calls,[])

    def test_ambiguous_cleanup_never_falls_back_to_pre_cleanup_view(self):
        self.assertEqual(self.guard(lambda:self.payload(84.42),call=lambda *_:1),1)
        self.assertEqual(self.calls,[])

    def test_manual_run_retains_explicit_deep_refresh_within_active_slot(self):
        self.assertEqual(self.guard(lambda:self.payload(fresh=True),slot="",manual=True),0)
        self.assertEqual(len(self.calls),2)
        self.assertEqual(active_slot(datetime(2026,11,23,0,3,tzinfo=timezone.utc)).isoformat(),
                         "2026-11-22T23:37:00+00:00")

    @unittest.skipUnless(shutil.which("jq"), "Bash integration needs jq")
    def test_shell_daily_post_no_retries_and_requires_exact_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            mock = pathlib.Path(tmp) / "curl"
            mock.write_text("""#!/bin/bash
printf '%s\n' "$@" > "$CALL_LOG"
printf '%s' "$FAKE_RESPONSE"
""")
            mock.chmod(0o700)
            log = pathlib.Path(tmp) / "log"
            env = {**os.environ, "PATH":tmp+":"+os.environ["PATH"], "CALL_LOG":str(log),
                   "SECURUS_URL":"https://example.invalid", "SECURUS_OIDC_TOKEN":"fake"}
            command=["bash",str(ROOT/"scripts/collect_source.sh"),"mode=daily&source=nflverse"]
            for source, ok in [("nflverse",True),("baseball-savant",False)]:
                env["FAKE_RESPONSE"]='{"accepted":true,"recordsWritten":1,"results":[{"status":"SUCCEEDED","sourceId":"'+source+'"}]}'
                completed=subprocess.run(command,env=env,capture_output=True,timeout=5)
                self.assertEqual(completed.returncode==0,ok)
                args=log.read_text().splitlines()
                self.assertEqual(args[args.index("--retry")+1],"0")
                self.assertEqual(args[args.index("--max-time")+1],"120")
                self.assertNotIn("--retry-all-errors",args)

    def test_workflow_preserves_pr8_sequence_and_fail_closed_freshness(self):
        flow=(ROOT/".github/workflows/securus-scheduler.yml").read_text()
        self.assertEqual(flow.count("mode=fast&source=propline"),1)
        parts=["Recheck bounded storage after collection",
               "Check recent measured capacity before quote collection",
               "Refresh independent player prop sportsbook prices after cleanup",
               "Verify this cycle's durable PropLine receipt",
               "Run one verified Crypto paper scan",
               "Refresh due deep statistics with measured headroom",
               "Surface stale decision inputs after the scan"]
        self.assertEqual([flow.index(p) for p in parts],sorted(flow.index(p) for p in parts))
        self.assertIn("steps.freshness.outcome != 'success'",flow)
        self.assertNotIn("mode=daily&source=",flow)


if __name__ == "__main__":
    unittest.main()

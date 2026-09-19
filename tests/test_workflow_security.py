from pathlib import Path
import unittest


WORKFLOW = Path(__file__).parents[1] / ".github" / "workflows" / "securus-scheduler.yml"


def job_block(source: str, job_id: str, next_job_id: str | None = None) -> str:
    start = source.index(f"  {job_id}:\n")
    if next_job_id is None:
        return source[start:]
    end = source.index(f"  {next_job_id}:\n", start + 1)
    return source[start:end]


class WorkflowSecurityTests(unittest.TestCase):
    def test_watchdog_checks_ten_minutes_and_trusted_completions_without_oidc(self):
        watchdog = (WORKFLOW.parent / "securus-watchdog.yml").read_text(encoding="utf-8")
        self.assertIn('cron: "2,12,22,32,42,52 * * * *"', watchdog)
        self.assertIn('workflows: ["Securus public scheduler"]', watchdog)
        self.assertIn("github.event.workflow_run.head_repository.full_name == github.repository", watchdog)
        self.assertIn("github.event.workflow_run.head_branch == 'main'", watchdog)
        self.assertIn('["push","schedule","workflow_dispatch"]', watchdog)
        self.assertNotIn("id-token:", watchdog)
        self.assertNotIn("--grace-minutes 15", watchdog)
        self.assertIn("cancel-in-progress: false", watchdog)

    @classmethod
    def setUpClass(cls):
        cls.workflow = WORKFLOW.read_text(encoding="utf-8")
        cls.collect = job_block(cls.workflow, "collect-inputs", "verify-nba-pdf")
        cls.verify = job_block(cls.workflow, "verify-nba-pdf", "collect-and-scan")
        cls.final = job_block(cls.workflow, "collect-and-scan")

    def test_native_pdf_parser_job_has_no_oidc_capability(self):
        self.assertIn("permissions:\n      contents: read", self.verify)
        self.assertNotIn("id-token:", self.verify)
        self.assertNotIn("SECURUS_OIDC_TOKEN", self.verify)
        self.assertIn("persist-credentials: false", self.verify)
        self.assertIn("assert_credential_free_environment", (
            Path(__file__).parents[1] / "scripts" / "nba_injury_attestation.py"
        ).read_text(encoding="utf-8"))

    def test_authenticated_jobs_only_exchange_bounded_job_outputs(self):
        self.assertIn("id-token: write", self.collect)
        self.assertIn("id-token: write", self.final)
        self.assertIn("nba_attestation_challenge:", self.collect)
        self.assertIn("nba_attestation:", self.verify)
        self.assertNotIn("report.txt", self.workflow)
        self.assertNotIn("upload-artifact", self.workflow)

    def test_final_job_name_preserves_watchdog_completion_contract(self):
        self.assertTrue(self.final.startswith("  collect-and-scan:\n"))
        self.assertIn("    name: collect-and-scan\n", self.final)
        self.assertIn("needs: [cadence-gate, collect-inputs, verify-nba-pdf]", self.final)
        self.assertIn("!cancelled()", self.final)

    def test_nba_collection_and_freshness_are_isolated_per_sport(self):
        hosted_loop = self.collect.split(
            "for source in ", 1
        )[1].split("; do", 1)[0]
        self.assertNotIn("nba-official-injuries", hosted_loop)
        self.assertIn(
            'collect_source.sh "mode=fast&source=nba-official-injuries"',
            self.collect,
        )
        freshness = self.final.split(
            "Require fresh non-NBA decision inputs", 1
        )[1].split("Run one verified Crypto paper scan", 1)[0]
        self.assertNotIn("nba-official-injuries", freshness)
        self.assertIn("--source nba-stats", freshness)

    def test_hosted_collectors_refresh_identity_after_maintenance(self):
        maintenance = job_block(self.workflow, "maintain-storage", "cadence-gate")
        self.assertIn("Enforce bounded storage maintenance", maintenance)
        self.assertNotIn("continue-on-error", maintenance)
        self.assertIn("python scripts/maintain_storage.py", maintenance)
        self.assertIn("--target-percent ${{ inputs.maintenance_only && '74' || '83.5' }}", maintenance)
        gate = job_block(self.workflow, "cadence-gate", "keepalive")
        self.assertIn("inputs.maintenance_only != true", gate)
        self.assertIn("needs: maintain-storage", self.workflow)
        refresh_offset = self.collect.index("Refresh identity after storage maintenance")
        hosted_offset = self.collect.index("Refresh hosted decision feeds")
        self.assertLess(refresh_offset, hosted_offset)
        hosted_block = self.collect[hosted_offset:self.collect.index(
            "Refresh official NBA injury availability"
        )]
        self.assertIn(
            "SECURUS_OIDC_TOKEN: ${{ steps.oidc_collect.outputs.token }}",
            hosted_block,
        )

    def test_scheduler_serializes_runtime_while_watchdog_stays_non_cancelling(self):
        watchdog = (WORKFLOW.parent / "securus-watchdog.yml").read_text(encoding="utf-8")
        self.assertIn("securus-public-runtime", self.workflow)
        self.assertIn("group: securus-public-watchdog", watchdog)
        self.assertIn("cancel-in-progress: false", self.workflow)
        self.assertIn("cancel-in-progress: false", watchdog)

    def test_unassessable_nba_does_not_suppress_other_sport_scan(self):
        scan_offset = self.final.index("Run one verified Crypto paper scan")
        surface_offset = self.final.index("Surface an unassessable NBA attestation")
        self.assertLess(scan_offset, surface_offset)
        self.assertIn("steps.nba_attestation.outcome == 'success'", self.final)

    def test_freshness_failure_is_surfaced_only_after_fail_closed_scan(self):
        freshness_offset = self.final.index(
            "Require fresh non-NBA decision inputs"
        )
        scan_offset = self.final.index("Run one verified Crypto paper scan")
        sentinel_offset = self.final.index(
            "Surface stale decision inputs"
        )
        self.assertLess(freshness_offset, scan_offset)
        self.assertLess(scan_offset, sentinel_offset)
        freshness = self.final[freshness_offset:scan_offset]
        self.assertIn("id: freshness", freshness)
        self.assertIn("continue-on-error: true", freshness)
        sentinel = self.final[sentinel_offset:]
        self.assertIn("always()", sentinel)
        self.assertIn("steps.freshness.outcome != 'success'", sentinel)


if __name__ == "__main__":
    unittest.main()

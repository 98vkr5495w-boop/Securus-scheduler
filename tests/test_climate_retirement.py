import os
from pathlib import Path
import unittest
from unittest.mock import patch

from scripts import collect_kalshi
from scripts.check_scheduler_due import GATED_SOURCES
from scripts.verify_freshness import MAX_AGE_MINUTES


class ClimateRetirementTests(unittest.TestCase):
    def test_reviewed_restart_is_manual_only(self):
        workflow = (Path(__file__).parents[1] / ".github/workflows/securus-reviewed-restart.yml").read_text()
        self.assertIn("  workflow_dispatch:", workflow)
        self.assertNotIn("  push:", workflow)
        self.assertNotIn("  schedule:", workflow)
        self.assertIn("if: github.event_name == 'workflow_dispatch'", workflow)

    def test_climate_is_not_a_scheduler_or_freshness_dependency(self):
        self.assertNotIn("climate", GATED_SOURCES)
        self.assertNotIn("climate", MAX_AGE_MINUTES)
        workflow = (Path(__file__).parents[1] / ".github/workflows/securus-scheduler.yml").read_text()
        self.assertNotIn("KEYLESS_SPORT: CLIMATE", workflow)
        self.assertNotIn("--source climate", workflow)
        self.assertIn("--source open-meteo", workflow)

    def test_legacy_climate_entry_point_performs_no_requests(self):
        with patch.dict(os.environ, {"KEYLESS_SPORT": "CLIMATE"}), \
             patch.object(collect_kalshi, "fetch_json") as fetch, \
             patch.object(collect_kalshi, "post_json") as post, \
             patch.object(collect_kalshi, "sync_run") as sync:
            self.assertEqual(collect_kalshi.main(), 2)
            fetch.assert_not_called()
            post.assert_not_called()
            sync.assert_not_called()

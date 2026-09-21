import unittest
from pathlib import Path


class ResearchScheduleTests(unittest.TestCase):
    def test_optional_research_is_polled_without_becoming_a_price_gate(self):
        workflow = (Path(__file__).resolve().parents[1] / '.github/workflows/securus-scheduler.yml').read_text()
        step = workflow.split('- name: Refresh free NFL and MLB research feeds', 1)[1].split('- name:', 1)[0]
        self.assertIn('continue-on-error: true', step)
        self.assertIn('timeout-minutes: 4', step)
        self.assertIn('espn-nfl nfl-official-injuries rotoworld-nfl fangraphs-mlb cbs-mlb-injuries rotoballer-mlb', step)
        self.assertIn('::warning::', step)
        self.assertNotIn('climate', step)


if __name__ == '__main__':
    unittest.main()

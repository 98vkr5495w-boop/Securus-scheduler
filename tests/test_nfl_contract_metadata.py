import unittest

from scripts.collect_kalshi import SERIES, market_metadata


class NflContractMetadataTests(unittest.TestCase):
    def test_nfl_readiness_requires_core_moneyline_not_optional_spreads(self):
        self.assertEqual([s["ticker"] for s in SERIES["NFL"] if s.get("required")], ["KXNFLGAME"])

    def test_public_rules_and_scalar_settlement_survive_transport(self):
        primary = "If Philadelphia wins, then the market resolves to Yes."
        secondary = "If the game ends in a tie, the market will resolve to $0.50 for each team."
        data = market_metadata({
            "ticker": "KXNFLGAME-26SEP28PHICHI-PHI",
            "rules_primary": primary,
            "rules_secondary": secondary,
            "result": "scalar",
            "settlement_value_dollars": "0.5000",
            "settlement_ts": "2026-09-29T04:00:00Z",
        }, {"event_ticker": "KXNFLGAME-26SEP28PHICHI"},
            {"ticker": "KXNFLGAME", "market_family": "moneyline"},
            "settled", "quadratic_with_maker_fees", 1)
        self.assertEqual(data["rulesPrimary"], primary)
        self.assertEqual(data["rulesSecondary"], secondary)
        self.assertEqual(data["settlementValueDollars"], 0.5)
        self.assertEqual(data["settlementTimestamp"], "2026-09-29T04:00:00Z")
        self.assertEqual(data["feeMultiplier"], 1)

    def test_missing_terms_remain_missing_not_invented(self):
        data = market_metadata({}, {}, {"ticker": "KXNFLGAME", "market_family": "moneyline"},
                               "open", "quadratic", 1)
        self.assertEqual(data["rulesPrimary"], "")
        self.assertEqual(data["rulesSecondary"], "")
        self.assertIsNone(data["settlementValueDollars"])

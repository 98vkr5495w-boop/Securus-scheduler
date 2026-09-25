import io
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from scripts import collect_kalshi_props as props
from scripts import collect_kalshi


class PropTransportTests(unittest.TestCase):
    def test_failure_diagnostic_redacts_response_and_request_details(self):
        transport = props.OfficialTransport()
        output = io.StringIO()
        error = HTTPError("https://example.invalid/?secret=private", 403, "private response", {}, io.BytesIO(b"private token"))
        with patch.object(props, "urlopen", side_effect=error), patch.object(props.sys, "stderr", output):
            props.capture("NFL", transport)
        self.assertIn('"code": "HTTP_403"', output.getvalue())
        self.assertNotIn("private", output.getvalue())
        self.assertNotIn("secret", output.getvalue())
        self.assertEqual(len(output.getvalue().splitlines()), 1)

    def test_snapshot_size_failure_is_distinct_from_upstream_denial(self):
        class Fixture:
            def get(self, path):
                if path.startswith("/series/"):
                    return {"series": {"public_document": "x" * 1000}}
                return {"events": [], "milestones": []}
        output = io.StringIO()
        with patch.object(props, "MAX_BYTES", 1000), patch.object(props.sys, "stderr", output):
            snapshot = props.capture("NFL", Fixture())
        self.assertIn('"code": "OVERSIZED_SNAPSHOT"', output.getvalue())
        self.assertTrue(all(entry.get("error") == "UPSTREAM_UNAVAILABLE" for entry in snapshot["series"]))

    def test_failure_stops_provider_calls_across_sports(self):
        transport = props.OfficialTransport()
        with patch.object(props, "urlopen", side_effect=HTTPError("test", 429, "limited", {"Retry-After": "3600"}, io.BytesIO())) as opened:
            for sport in props.SERIES:
                snapshot = props.capture(sport, transport)
                self.assertTrue(all(entry.get("error") == "UPSTREAM_UNAVAILABLE" for entry in snapshot["series"]))
            self.assertEqual(opened.call_count, 1)

    def test_raw_targets_are_transported_without_interpretation(self):
        class Fixture:
            def get(self, path):
                if path.startswith("/series/"):
                    return {"series": {"fee_type": "quadratic", "fee_multiplier": 1}}
                if path.startswith("/events?"):
                    return {"events": [{"markets": [{"custom_strike": {"baseball_player": "p", "baseball_team": "t"}}]}], "milestones": [{"details": {"home_team_id": "t", "away_team_id": "a"}}]}
                self.query = path
                return {"structured_targets": [{"id": "p", "name": "Fixture Player"}]}
        transport = Fixture()
        snapshot = props.capture("MLB", transport)
        self.assertEqual(len(snapshot["series"]), 2)
        self.assertIn("ids=p", transport.query)
        self.assertIn("ids=a", transport.query)
        self.assertEqual(snapshot["series"][0]["targets"]["structured_targets"][0]["name"], "Fixture Player")
        self.assertNotIn("verified", str(snapshot))

    def test_generic_cycle_does_not_poll_mlb_props_twice(self):
        with patch.dict(collect_kalshi.os.environ, {"KALSHI_PROPS_TRANSPORT": "separate"}), patch.object(collect_kalshi, "collect_series", return_value=([], [], [])) as collect:
            collect_kalshi.collect_sport("MLB")
            self.assertTrue(collect.call_count > 0)
            self.assertTrue(all(call.args[1].get("market_family") != "player_prop" for call in collect.call_args_list))

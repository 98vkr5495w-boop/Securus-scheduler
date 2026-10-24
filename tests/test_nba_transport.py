import io
import json
import unittest
from unittest.mock import patch
from scripts import collect_nba, collect_kalshi_props

class NbaTransportTests(unittest.TestCase):
    def test_exact_official_document_and_capture_time_are_preserved(self):
        document = {"meta": {"code": 200}, "scoreboard": {"leagueId": "00", "games": []}}
        with patch.object(collect_nba, 'urlopen', return_value=io.BytesIO(json.dumps(document).encode())) as read:
            payload = collect_nba.capture()
        self.assertEqual(read.call_args.args[0].full_url, collect_nba.URL)
        self.assertEqual(payload['scoreboard'], document['scoreboard'])
        self.assertTrue(payload['capturedAt'].endswith('Z'))

    def test_failed_or_oversized_documents_are_never_submitted(self):
        for failure in [OSError('private details'), None]:
            with patch.object(collect_nba, 'urlopen', side_effect=failure, return_value=io.BytesIO(b'x' * (collect_nba.MAX_BYTES + 1))), patch.object(collect_nba, 'post_json') as post, patch.object(collect_nba.sys, 'stdout', io.StringIO()) as output:
                self.assertEqual(collect_nba.main(), 1)
                post.assert_not_called()
                self.assertNotIn('private', output.getvalue())

    def test_basketball_structured_targets_use_basketball_identity(self):
        class Transport:
            def get(self, path):
                if path.startswith('/series/'):
                    return {'series': {}}
                if path.startswith('/events?'):
                    return {'events': [{'markets': [{'custom_strike': {'basketball_player': 'p', 'basketball_team': 't'}}]}], 'milestones': []}
                self.targets = path
                return {'structured_targets': [{'id': 'p'}, {'id': 't'}]}
        t = Transport()
        result = collect_kalshi_props.capture('NBA', t, 'KXNBAPTS')
        self.assertIn('ids=p', t.targets)
        self.assertIn('ids=t', t.targets)
        self.assertEqual(len(result['series'][0]['targets']['structured_targets']), 2)

"""v3 per-game policy without weakening transport or v2 compatibility.

Real fixture: official Injury-Report_2026-10-25_11_30AM.pdf, extracted with
pdftotext -layout. PDF SHA256:
df21c386d5d8a3d464e5b781eb0d60225dd86e0cde1c3c680182b56661d53658.
"""
import copy
from pathlib import Path
import unittest
from scripts.nba_injury_attestation import (AttestationError, analyze_extracted_text, analyze_team_blocks_v3, validate_challenge, validate_attestation, verify_challenge, submit_attestation)
from tests.test_nba_injury_attestation import challenge, extracted_text, FakeOpener, FakeResponse, FakePdftotext, PDF_BYTES


def v3(text=None):
    target = challenge()
    target.update(schemaVersion=3, verifierVersion='securus-nba-completeness-v3')
    return verify_challenge(target, pdf_opener=FakeOpener(FakeResponse(PDF_BYTES)), runner=FakePdftotext(text or extracted_text()))


class V3Tests(unittest.TestCase):
    def test_v2_fallback_and_v3_negotiation(self):
        self.assertEqual(validate_challenge(challenge())['schemaVersion'], 2)
        a = v3()
        self.assertEqual(a['schemaVersion'], 3)
        self.assertEqual(a['matchupEvidence'][0]['teams'][0], {'team':'IND','submission':'SUBMITTED','blockVerified':True})
        self.assertTrue(a['teamBlocksComplete'])
        self.assertNotIn('complete', a)
        mixed = challenge(); mixed['schemaVersion'] = 3
        with self.assertRaises(AttestationError): validate_challenge(mixed)

    def test_real_pdf_exact_two_unsubmitted_teams_and_five_remaining_games(self):
        text = (Path(__file__).parent/'fixtures/nba-injury-2026-10-25-1130.txt').read_text()
        finding = analyze_extracted_text(text, '2026-10-25', '2026-10-25T15:30:00Z')
        self.assertTrue(finding['schemaAnchorsVerified'])
        rows = analyze_team_blocks_v3(text, '2026-10-25')
        self.assertEqual(len(rows), 7)
        pending = [t['team'] for r in rows for t in r['teams'] if t['submission']=='NOT_YET_SUBMITTED']
        self.assertEqual(sorted(pending), ['BKN','PHI'])
        eligible = [r['matchup'] for r in rows if all(t['submission']=='SUBMITTED' and t['blockVerified'] for t in r['teams'])]
        self.assertEqual(eligible, ['LAC@OKC','LAL@UTA','MEM@SAC','ORL@NYK','TOR@MIN'])

    def test_unknown_missing_duplicate_conflicting_blocks_never_submitted(self):
        base = extracted_text()
        for text in [base.replace('New York Knicks','Chicago Bulls'),base.replace('New York Knicks Example, Player Available','New York Knicks'),base.replace('New York Knicks Example, Player Available','New York Knicks NOT YET SUBMITTED Example, Player Available'),base.replace('New York Knicks Example, Player Available','New York Knicks Example, Player Mystery'),base.replace('New York Knicks Example, Player Available','New York Knicks Example, Player Available\nNew York Knicks')]:
            a=v3(text)
            self.assertFalse(a['teamBlocksComplete'])
            self.assertTrue(any(t['submission']=='UNKNOWN' and not t['blockVerified'] for t in a['matchupEvidence'][0]['teams']))

    def test_partial_submission_keeps_global_complete_false_and_checks_response(self):
        a=v3(extracted_text().replace('New York Knicks Example, Player Available','New York Knicks NOT YET SUBMITTED'))
        self.assertFalse(a['teamBlocksComplete']); self.assertEqual(a['notYetSubmittedCount'],1)
        response={'required':True,'stored':True,'complete':False,'sha256':a['sha256'],'verifierVersion':a['verifierVersion'],'eligibleMatchups':[],'blockedMatchups':['IND@NYK']}
        self.assertFalse(submit_attestation('https://site.example','token',a,requester=lambda *args,**kw:response)['complete'])
        for patch in [{'complete':True},{'eligibleMatchups':['IND@NYK']},{'blockedMatchups':[]}]:
            with self.assertRaises(AttestationError):submit_attestation('https://site.example','token',a,requester=lambda *args,**kw:{**response,**patch})

    def test_v3_rejects_schema_contradictions(self):
        a=v3()
        for mutate in [lambda x:x.update(teamBlocksComplete=False), lambda x:x.update(notYetSubmittedCount=1),lambda x:x['matchupEvidence'][0]['teams'][0].update(team='BOS'),lambda x:x['matchupEvidence'][0]['teams'][0].update(submission='UNKNOWN'),lambda x:x['matchupEvidence'].append(copy.deepcopy(x['matchupEvidence'][0]))]:
            b=copy.deepcopy(a);mutate(b)
            with self.assertRaises(AttestationError):validate_attestation(b)

class NegotiationTests(unittest.TestCase):
    def test_explicit_fixed_get_query_accepts_old_v2_and_new_v3_challenges(self):
        import json
        from scripts.nba_injury_attestation import fetch_challenge, site_request_json
        for version in (2, 3):
            payload=challenge()
            payload.update(schemaVersion=version,verifierVersion=f'securus-nba-completeness-v{version}')
            opener=FakeOpener(FakeResponse(json.dumps(payload).encode(),status=200,content_type='application/json'))
            result=fetch_challenge('https://securus.example','oidc',requester=lambda base,token,**kw:site_request_json(base,token,opener=opener,**kw))
            self.assertEqual(result['schemaVersion'],version)
            self.assertEqual(len(opener.requests),1)
            request,_=opener.requests[0]
            self.assertEqual(request.full_url,'https://securus.example/api/nba-injury-attestation?schemaVersion=3')
            self.assertEqual(request.get_method(),'GET')
            self.assertEqual(request.get_header('Authorization'),'Bearer oidc')

    def test_post_target_stays_exact_and_origin_query_injection_is_rejected(self):
        from scripts.nba_injury_attestation import site_request_json
        opener=FakeOpener(FakeResponse(b'{"stored":true}',status=201,content_type='application/json'))
        site_request_json('https://securus.example','oidc',method='POST',payload={'schemaVersion':3},opener=opener)
        request,_=opener.requests[0]
        self.assertEqual(request.full_url,'https://securus.example/api/nba-injury-attestation')
        self.assertEqual(request.get_method(),'POST')
        for origin in ['https://securus.example?schemaVersion=9','https://securus.example/api/nba-injury-attestation','https://securus.example#fragment']:
            with self.assertRaises(AttestationError):site_request_json(origin,'oidc',method='GET',opener=opener)
        self.assertEqual(len(opener.requests),1)

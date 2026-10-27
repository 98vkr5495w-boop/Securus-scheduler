"""Official text fixtures: Oct26 19:30 PDF SHA256
5cfd39a4613a46d4349d074a594bd92a73f0c146781a519853ca66150c7e2370;
Oct25 17:00 PDF SHA256
a0a6c9717b481839d2eaea10bc01e2f22ec5e87dcfe7cd1035ed5b062fb87f33.
These are layout evidence, not medical inference or missing=healthy labels.
"""
import copy
from pathlib import Path
import unittest
from scripts.nba_injury_attestation import (AttestationError, analyze_extracted_text, analyze_team_blocks_v4, validate_attestation, verify_challenge, submit_attestation)
from tests.test_nba_injury_attestation import challenge, FakeOpener, FakeResponse, FakePdftotext, PDF_BYTES
FIX=Path(__file__).parent/'fixtures'
MISSING=(FIX/'nba-injury-2026-10-26-1930.txt').read_text()
COMPLETE=(FIX/'nba-injury-2026-10-25-1700.txt').read_text()
DATE='2026-10-26';STAMP='2026-10-26T23:30:00Z'
EXPECTED=['ATL@HOU','CHI@BOS','DAL@MIA','GSW@DEN','IND@MIL','MEM@UTA','MIN@CLE','PHX@OKC','WAS@CHA']
def audit(text=MISSING, expected=None, stamp=STAMP):
    return analyze_team_blocks_v4(text,DATE,stamp,expected or EXPECTED)
def evidence(text=MISSING):
    c=challenge();c.update(schemaVersion=4,verifierVersion='securus-nba-completeness-v4')
    c['target'].update(reportDate=DATE,targetGameDate=DATE,reportPublishedAt=STAMP,expectedMatchups=EXPECTED,reportUrl='https://ak-static.cms.nba.com/referee/injury/Injury-Report_2026-10-26_07_30PM.pdf')
    # Fake downloader validates challenge hash/bytes unchanged; real fixture
    # is independently retained with provenance above, no public submission.
    return verify_challenge(c,pdf_opener=FakeOpener(FakeResponse(PDF_BYTES,url=c['target']['reportUrl'])),runner=FakePdftotext(text))

class V4Tests(unittest.TestCase):
    def test_real_missing_mil_localized_and_ind_continuation_preserved(self):
        rows,valid=audit();self.assertTrue(valid)
        self.assertEqual(len(rows),9)
        missing=[(r['matchup'],t['team']) for r in rows for t in r['teams'] if t['submission']=='MISSING_SECTION']
        self.assertEqual(missing,[('IND@MIL','MIL')])
        self.assertEqual(sum(all(t['submission']=='SUBMITTED' for t in r['teams']) for r in rows),8)
        a=evidence();self.assertFalse(a['teamBlocksComplete']);self.assertTrue(a['localizedMissingSectionsVerified']);self.assertEqual(a['notYetSubmittedCount'],0)

    def test_real_complete_report_and_pagebreak_continuations(self):
        day='2026-10-25';stamp='2026-10-25T21:00:00Z';expected=analyze_extracted_text(COMPLETE,day,stamp)['reportedMatchups']
        rows,valid=analyze_team_blocks_v4(COMPLETE,day,stamp,expected);self.assertTrue(valid);self.assertEqual(len(rows),7)
        self.assertTrue(all(t['submission']=='SUBMITTED' and t['blockVerified'] for r in rows for t in r['teams']))

    def test_multiple_entire_missing_sections_are_local_but_dropped_heading_rows_are_not(self):
        # Boston has exactly one row; remove the entire explicitly bounded
        # team section, not just the heading that would orphan its player.
        line=next(l for l in MISSING.splitlines() if 'Boston Celtics' in l)
        rows,valid=audit(MISSING.replace(line,''));self.assertTrue(valid)
        self.assertEqual(sorted(t['team'] for r in rows for t in r['teams'] if t['submission']=='MISSING_SECTION'),['BOS','MIL'])
        # Unknown heading at the team column is never swallowed as player text.
        self.assertFalse(audit(MISSING.replace('Boston Celtics','Boston Unknown'))[1])

    def test_page_integrity_header_timestamp_and_full_slate(self):
        cases=[MISSING.replace('Page 2 of 6','Page 1 of 6'),MISSING.replace('Page 2 of 6','TEMP').replace('Page 3 of 6','Page 2 of 6').replace('TEMP','Page 3 of 6'),MISSING.replace('Page 2 of 6',''),MISSING.replace('Page 2 of 6','Page 2 of 6\nPage 2 of 6'),MISSING.replace('\f','\f\f',1),MISSING+'\f',MISSING.replace('\f','',1),MISSING.replace('Injury Report: 10/26/26 07:30 PM','Injury Report: 10/26/26 07:00 PM',1),MISSING.replace('IND@MIL','IND@MIL IND@MIL',1)]
        for text in cases:self.assertFalse(audit(text)[1])
        self.assertFalse(audit(expected=EXPECTED+['LAL@NYK'])[1])
        self.assertFalse(audit(stamp='2026-10-26T23:00:00Z')[1])
        pages=MISSING.split('\f');pages[1],pages[2]=pages[2],pages[1];self.assertFalse(audit('\f'.join(pages))[1])

    def test_exactly_one_terminal_formfeed_is_required(self):
        # Real pdftotext emits one final page separator, never zero or two.
        self.assertTrue(MISSING.endswith('\f'))
        self.assertTrue(audit(MISSING)[1])
        for text in (MISSING[:-1], MISSING + '\f', MISSING + '\n\f'):
            with self.subTest(tail=repr(text[-20:])):
                rows, valid = audit(text)
                self.assertFalse(valid)
                self.assertFalse(any(t['submission'] == 'MISSING_SECTION' for r in rows for t in r['teams']))

    def test_unknown_status_or_orphan_player_pending_or_heading_globally_blocks(self):
        cases=[MISSING.replace('DiVincenzo, Donte   Out','Doe, John          Suspended'),MISSING.replace('10/26/2026','02/30/2026',1),MISSING.replace('Page 6 of 6','                      Unknown trailing text\nPage 6 of 6'),MISSING.replace('Game Date    Game Time', 'Orphan, Player   Out\nGame Date    Game Time'),MISSING.replace('Game Date    Game Time','NOT YET SUBMITTED\nGame Date    Game Time'),MISSING.replace('Boston Celtics','Milwaukee Bucks'),MISSING.replace('Boston Celtics','Boston Celtics Boston Celtics'),MISSING.replace('Page 6 of 6','Orphan, Player   Unknown\nPage 6 of 6')]
        for text in cases:
            rows,valid=audit(text);self.assertFalse(valid);self.assertFalse(any(t['submission']=='MISSING_SECTION' for r in rows for t in r['teams']))

    def test_server_response_and_proof_flags_cannot_promote_missing_team(self):
        a=evidence();eligible=[m for m in EXPECTED if m!='IND@MIL']
        response={'required':True,'stored':True,'complete':False,'sha256':a['sha256'],'verifierVersion':a['verifierVersion'],'eligibleMatchups':eligible,'blockedMatchups':['IND@MIL']}
        self.assertFalse(submit_attestation('https://site.example','token',a,requester=lambda *args,**kw:response)['complete'])
        with self.assertRaises(AttestationError):submit_attestation('https://site.example','token',a,requester=lambda *args,**kw:{**response,'eligibleMatchups':EXPECTED})
        for mutate in [lambda x:x.update(localizedMissingSectionsVerified=False),lambda x:x.update(schemaAnchorsVerified=False),lambda x:x.update(teamBlocksComplete=True),lambda x:x['matchupEvidence'][4]['teams'][1].update(blockVerified=True)]:
            x=copy.deepcopy(a);mutate(x)
            with self.assertRaises(AttestationError):validate_attestation(x)

    def test_unverified_localization_cannot_approve_even_all_submitted_payload(self):
        a=evidence()
        a['matchupEvidence'][4]['teams'][1].update(submission='SUBMITTED',blockVerified=True)
        a.update(teamBlocksComplete=True,localizedMissingSectionsVerified=False)
        validate_attestation(a)
        response={'required':True,'stored':True,'complete':False,'sha256':a['sha256'],'verifierVersion':a['verifierVersion'],'eligibleMatchups':[],'blockedMatchups':EXPECTED}
        self.assertFalse(submit_attestation('https://site.example','token',a,requester=lambda *args,**kw:response)['complete'])

    def test_oversized_page_claim_is_bounded_and_failed(self):
        self.assertEqual(audit(MISSING.replace('of 6','of 999999999')),([],False))
        with self.assertRaises(AttestationError):evidence(MISSING.replace('of 6','of 999999999'))

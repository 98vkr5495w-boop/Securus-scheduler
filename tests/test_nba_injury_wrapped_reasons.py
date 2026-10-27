"""Exact official 2026-10-27 13:45 ET report, PDF SHA256:
0ea318ba35a9ba91e0e300f356241cb751daf8802ef1ed5c8eea72285d954da9.
Official URL: https://ak-static.cms.nba.com/referee/injury/Injury-Report_2026-10-27_01_45PM.pdf
Tomorrow's rows remain part of the full-document integrity audit.
"""
from pathlib import Path
import unittest
from scripts.nba_injury_attestation import analyze_team_blocks_v4

TEXT = (Path(__file__).parent / 'fixtures/nba-injury-2026-10-27-1345.txt').read_text()
EXPECTED = ['BKN@BOS', 'DET@NYK', 'POR@LAL', 'SAC@SAS']
PREFIX = ' ' * 78 + 'Injury/Illness - Right Low Grade High'
TAIL = ' ' * 78 + 'Ankle Sprain'
PLAYER = next(line for line in TEXT.splitlines() if 'Banchero, Paolo' in line)

def audit(text=TEXT, expected=EXPECTED, stamp='2026-10-27T17:45:00Z'):
    return analyze_team_blocks_v4(text, '2026-10-27', stamp, expected)

class WrappedReasonTests(unittest.TestCase):
    def test_actual_document_all_four_target_games_submitted(self):
        rows, valid = audit()
        self.assertTrue(valid)
        self.assertEqual([row['matchup'] for row in rows], EXPECTED)
        self.assertTrue(all(t['submission'] == 'SUBMITTED' and t['blockVerified'] for r in rows for t in r['teams']))

    def test_wrapped_block_requires_exact_alignment_adjacency_and_status(self):
        cases = {
            'prefix misaligned': TEXT.replace(PREFIX, ' ' + PREFIX),
            'tail misaligned': TEXT.replace(TAIL, ' ' + TAIL),
            'unknown status': TEXT.replace(PLAYER, PLAYER.replace('Out', 'Suspended')),
            'unknown player row': TEXT.replace(PLAYER, PLAYER.replace('Banchero, Paolo', 'Unclassified text')),
            'orphan prefix': TEXT.replace(PLAYER, ''),
            'missing prefix': TEXT.replace(PREFIX, ''),
            'missing tail': TEXT.replace(TAIL, ''),
            'extra continuation': TEXT.replace(TAIL, TAIL + '\n' + ' ' * 78 + 'Unexpected extra content'),
            'orphan player': TEXT.replace(TAIL, TAIL + '\n' + ' ' * 78 + 'Doe, John  Out'),
            'unknown prefix': TEXT.replace(PREFIX, ' ' * 78 + 'Unknown unassigned text'),
            'pending column conflict': TEXT.replace('Indiana Pacers                                         NOT YET', 'Indiana Pacers                                          NOT YET'),
            'pending trailing text': TEXT.replace('Indiana Pacers                                         NOT YET SUBMITTED', 'Indiana Pacers                                         NOT YET SUBMITTED arbitrary'),
            'wrong team': TEXT.replace('Orlando Magic', 'Boston Celtics'),
            'unknown team': TEXT.replace('Orlando Magic', 'Unknown Team'),
        }
        for name, text in cases.items():
            with self.subTest(name=name):
                self.assertNotEqual(text, TEXT)
                self.assertFalse(audit(text)[1])

    def test_page_slate_date_and_duplicate_checks_still_fail_closed(self):
        for text in (TEXT.replace('Page 3 of 4', 'Page 2 of 4'), TEXT + '\f', TEXT[:-1], TEXT.replace('ORL@TOR', 'ORL@TOR ORL@TOR'), TEXT.replace('10/28/2026', '02/30/2026'), TEXT.replace('Injury Report: 10/27/26 01:45 PM', 'Injury Report: 10/27/26 01:30 PM', 1)):
            with self.subTest(tail=text[-20:]):
                self.assertFalse(audit(text)[1])
        self.assertFalse(audit(expected=EXPECTED + ['IND@MIL'])[1])
        self.assertFalse(audit(stamp='2026-10-27T17:30:00Z')[1])

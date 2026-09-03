from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.nba_injury_attestation import (
    AttestationError,
    PARSER_ID,
    SCHEMA_VERSION,
    VERIFIER_VERSION,
    analyze_extracted_text,
    assert_credential_free_environment,
    decode_payload,
    download_exact_pdf,
    encode_payload,
    site_request_json,
    submit_attestation,
    validate_attestation,
    validate_challenge,
    verify_challenge,
)


PDF_BYTES = b"%PDF-1.4\n" + (b"0" * 8_200) + b"\n%%EOF\n"
PDF_SHA256 = hashlib.sha256(PDF_BYTES).hexdigest()
REPORT_URL = (
    "https://ak-static.cms.nba.com/referee/injury/"
    "Injury-Report_2026-02-10_10_00AM.pdf"
)
REPORT_PUBLISHED_AT = "2026-02-10T15:00:00Z"
ATTESTATION_FIELDS = {
    "schemaVersion",
    "verifierVersion",
    "reportUrl",
    "reportDate",
    "reportPublishedAt",
    "sha256",
    "bytes",
    "targetGameDate",
    "expectedMatchups",
    "scheduleCapturedAt",
    "scheduleCompletedAt",
    "parserId",
    "parserVersion",
    "extractionSucceeded",
    "schemaAnchorsVerified",
    "targetDateSectionFound",
    "reportedMatchups",
    "teamBlocksComplete",
    "targetMatchupCount",
    "notYetSubmittedCount",
    "textBytes",
}


def challenge(required: bool = True) -> dict:
    if not required:
        return {"required": False}
    return {
        "required": True,
        "schemaVersion": SCHEMA_VERSION,
        "verifierVersion": VERIFIER_VERSION,
        "target": {
            "reportUrl": REPORT_URL,
            "reportDate": "2026-02-10",
            "reportPublishedAt": REPORT_PUBLISHED_AT,
            "sha256": PDF_SHA256,
            "bytes": len(PDF_BYTES),
            "targetGameDate": "2026-02-10",
            "expectedMatchups": ["IND@NYK"],
            "scheduleCapturedAt": "2026-02-10T14:58:00Z",
            "scheduleCompletedAt": "2026-02-10T14:59:00Z",
        },
    }


def extracted_text(
    *,
    target_missing: bool = False,
    header_date: str = "02/10/26",
    header_hour: str = "10:00",
    header_meridiem: str = "AM",
) -> str:
    target_date = "02/09/2026" if target_missing else "02/10/2026"
    return f"""\
Injury Report: {header_date} {header_hour} {header_meridiem}
Game Date    Game Time    Matchup    Team    Player Name    Current Status    Reason
{target_date} 07:30 (ET) IND@NYK Indiana Pacers Example, Player Out Rest
New York Knicks Example, Player Available
02/11/2026 07:00 (ET) ATL@CHA Atlanta Hawks NOT YET SUBMITTED
Charlotte Hornets NOT YET SUBMITTED
Page 1 of 1
"""


class FakeHeaders(dict):
    def get_content_type(self) -> str:
        return str(self.get("Content-Type", "")).split(";", 1)[0].lower()


class FakeResponse:
    def __init__(
        self,
        body: bytes,
        *,
        url: str = REPORT_URL,
        status: int = 200,
        content_type: str = "application/pdf",
        content_length: int | None = None,
    ):
        self._stream = io.BytesIO(body)
        self._url = url
        self._status = status
        self.headers = FakeHeaders({"Content-Type": content_type})
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def getcode(self) -> int:
        return self._status

    def geturl(self) -> str:
        return self._url

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)


class FakeOpener:
    def __init__(self, response: FakeResponse):
        self.response = response
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        return self.response


class FakePdftotext:
    def __init__(self, text: str, *, extract_returncode: int = 0):
        self.text = text
        self.extract_returncode = extract_returncode
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        if command == ["pdftotext", "-v"]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=b"",
                stderr=b"pdftotext version 24.02.0\n",
            )
        if self.extract_returncode == 0:
            Path(command[-1]).write_text(self.text, encoding="utf-8")
        return subprocess.CompletedProcess(
            command,
            self.extract_returncode,
            stdout=b"",
            stderr=b"",
        )


class ChallengeTests(unittest.TestCase):
    def test_offseason_challenge_is_reduced_to_required_false(self):
        self.assertEqual(
            validate_challenge({"required": False, "ignored": "server detail"}),
            {"required": False},
        )

    def test_active_challenge_requires_exact_public_evidence(self):
        self.assertEqual(validate_challenge(challenge()), challenge())

        cases = []
        upper_digest = challenge()
        upper_digest["target"]["sha256"] = PDF_SHA256.upper()
        cases.append(upper_digest)

        wrong_host = challenge()
        wrong_host["target"]["reportUrl"] = REPORT_URL.replace(
            "ak-static.cms.nba.com", "example.com"
        )
        cases.append(wrong_host)

        wrong_date = challenge()
        wrong_date["target"]["reportDate"] = "2026-02-11"
        cases.append(wrong_date)

        old_target_date = challenge()
        old_target_date["target"]["targetGameDate"] = "2026-02-08"
        cases.append(old_target_date)

        boolean_size = challenge()
        boolean_size["target"]["bytes"] = True
        cases.append(boolean_size)

        unsorted_matchups = challenge()
        unsorted_matchups["target"]["expectedMatchups"] = [
            "LAC@HOU",
            "IND@NYK",
        ]
        cases.append(unsorted_matchups)

        duplicate_matchups = challenge()
        duplicate_matchups["target"]["expectedMatchups"] = [
            "IND@NYK",
            "IND@NYK",
        ]
        cases.append(duplicate_matchups)

        invalid_schedule_time = challenge()
        invalid_schedule_time["target"]["scheduleCapturedAt"] = "not-a-time"
        cases.append(invalid_schedule_time)

        wrong_verifier = challenge()
        wrong_verifier["verifierVersion"] = "nba-availability-v1"
        cases.append(wrong_verifier)

        for invalid in cases:
            with self.subTest(invalid=invalid):
                with self.assertRaises(AttestationError):
                    validate_challenge(invalid)

    def test_challenge_carries_only_bounded_optional_header_evidence(self):
        server_challenge = challenge()
        server_challenge["target"]["filenamePublishedAt"] = REPORT_PUBLISHED_AT
        server_challenge["target"]["capturedAt"] = "2026-02-10T15:01:00Z"
        bounded = validate_challenge(server_challenge)
        self.assertEqual(
            bounded["target"]["filenamePublishedAt"], REPORT_PUBLISHED_AT
        )
        self.assertNotIn("capturedAt", bounded["target"])

    def test_job_output_round_trip_is_canonical_and_bounded(self):
        encoded = encode_payload(challenge())
        self.assertNotIn("=", encoded)
        self.assertEqual(decode_payload(encoded, "challenge"), challenge())
        with self.assertRaises(AttestationError):
            decode_payload("not+base64", "challenge")

    def test_site_transport_requires_method_specific_success_status(self):
        get_opener = FakeOpener(
            FakeResponse(
                b'{"required":false}',
                status=200,
                content_type="application/json",
            )
        )
        self.assertEqual(
            site_request_json(
                "https://securus.example",
                "oidc",
                method="GET",
                opener=get_opener,
            ),
            {"required": False},
        )
        post_opener = FakeOpener(
            FakeResponse(
                b'{"stored":true}',
                status=201,
                content_type="application/json",
            )
        )
        self.assertEqual(
            site_request_json(
                "https://securus.example",
                "oidc",
                method="POST",
                payload={"schemaVersion": SCHEMA_VERSION},
                opener=post_opener,
            ),
            {"stored": True},
        )
        with self.assertRaises(AttestationError):
            site_request_json(
                "https://securus.example",
                "oidc",
                method="POST",
                payload={},
                opener=FakeOpener(FakeResponse(b"{}", status=200)),
            )


class PdfDownloadTests(unittest.TestCase):
    def test_download_sends_no_credentials_and_matches_raw_digest(self):
        opener = FakeOpener(
            FakeResponse(PDF_BYTES, content_length=len(PDF_BYTES))
        )
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "report.pdf"
            download_exact_pdf(challenge()["target"], destination, opener=opener)
            self.assertEqual(destination.read_bytes(), PDF_BYTES)

        request, timeout = opener.requests[0]
        self.assertEqual(timeout, 30)
        headers = {key.lower(): value for key, value in request.header_items()}
        self.assertNotIn("authorization", headers)
        self.assertEqual(headers["accept"], "application/pdf")

    def test_download_fails_closed_on_hash_mime_size_or_url_change(self):
        invalid_responses = (
            FakeResponse(PDF_BYTES[:-1]),
            FakeResponse(PDF_BYTES, content_type="text/html"),
            FakeResponse(PDF_BYTES, url=REPORT_URL + "?changed=1"),
            FakeResponse(PDF_BYTES, content_length=len(PDF_BYTES) + 1),
        )
        for response in invalid_responses:
            with self.subTest(response=response):
                with tempfile.TemporaryDirectory() as temporary:
                    with self.assertRaises(AttestationError):
                        download_exact_pdf(
                            challenge()["target"],
                            Path(temporary) / "report.pdf",
                            opener=FakeOpener(response),
                        )


class TextAnalysisTests(unittest.TestCase):
    def test_future_date_not_submitted_rows_do_not_contaminate_target_date(self):
        findings = analyze_extracted_text(
            extracted_text(), "2026-02-10", REPORT_PUBLISHED_AT
        )
        self.assertEqual(
            findings,
            {
                "schemaAnchorsVerified": True,
                "targetDateSectionFound": True,
                "reportedMatchups": ["IND@NYK"],
                "teamBlocksComplete": True,
                "targetMatchupCount": 1,
                "notYetSubmittedCount": 0,
            },
        )

    def test_target_date_marker_is_counted_across_whitespace(self):
        text = extracted_text().replace(
            "New York Knicks Example, Player Available",
            "New York Knicks NOT\n      YET\tSUBMITTED",
        )
        findings = analyze_extracted_text(text, "2026-02-10", REPORT_PUBLISHED_AT)
        self.assertEqual(findings["notYetSubmittedCount"], 1)
        self.assertEqual(findings["targetMatchupCount"], 1)
        self.assertEqual(findings["reportedMatchups"], ["IND@NYK"])

    def test_missing_section_and_mismatched_header_are_explicit(self):
        missing = analyze_extracted_text(
            extracted_text(target_missing=True),
            "2026-02-10",
            REPORT_PUBLISHED_AT,
        )
        self.assertFalse(missing["targetDateSectionFound"])
        self.assertEqual(missing["targetMatchupCount"], 0)

        wrong_header = analyze_extracted_text(
            extracted_text(header_hour="11:00"),
            "2026-02-10",
            REPORT_PUBLISHED_AT,
        )
        self.assertFalse(wrong_header["schemaAnchorsVerified"])

    def test_duplicate_or_unknown_matchup_codes_invalidate_schema(self):
        duplicate = extracted_text().replace(
            "New York Knicks Example, Player Available",
            "08:00 (ET) IND@NYK New York Knicks Example, Player Available",
        )
        self.assertFalse(
            analyze_extracted_text(
                duplicate, "2026-02-10", REPORT_PUBLISHED_AT
            )["schemaAnchorsVerified"]
        )
        unknown = extracted_text().replace("IND@NYK", "XXX@NYK")
        self.assertFalse(
            analyze_extracted_text(
                unknown, "2026-02-10", REPORT_PUBLISHED_AT
            )["schemaAnchorsVerified"]
        )

    def test_every_matchup_requires_both_official_team_blocks(self):
        missing_home = extracted_text().replace(
            "New York Knicks Example, Player Available\n", ""
        )
        findings = analyze_extracted_text(
            missing_home, "2026-02-10", REPORT_PUBLISHED_AT
        )
        self.assertEqual(findings["targetMatchupCount"], 1)
        self.assertTrue(findings["schemaAnchorsVerified"])
        self.assertFalse(findings["teamBlocksComplete"])

    def test_missing_page_in_declared_sequence_invalidates_schema(self):
        truncated = extracted_text().replace("Page 1 of 1", "Page 1 of 2")
        self.assertFalse(
            analyze_extracted_text(
                truncated, "2026-02-10", REPORT_PUBLISHED_AT
            )["schemaAnchorsVerified"]
        )


class VerificationTests(unittest.TestCase):
    def test_end_to_end_verification_emits_only_exact_bounded_schema(self):
        runner = FakePdftotext(extracted_text())
        attestation = verify_challenge(
            challenge(),
            pdf_opener=FakeOpener(FakeResponse(PDF_BYTES)),
            runner=runner,
        )
        self.assertIsNotNone(attestation)
        assert attestation is not None
        self.assertEqual(set(attestation), ATTESTATION_FIELDS)
        self.assertEqual(attestation["schemaVersion"], SCHEMA_VERSION)
        self.assertEqual(attestation["verifierVersion"], VERIFIER_VERSION)
        self.assertEqual(attestation["parserId"], PARSER_ID)
        self.assertEqual(attestation["parserVersion"], "24.02.0")
        self.assertEqual(attestation["sha256"], PDF_SHA256)
        self.assertEqual(attestation["targetGameDate"], "2026-02-10")
        self.assertEqual(attestation["expectedMatchups"], ["IND@NYK"])
        self.assertEqual(attestation["reportedMatchups"], ["IND@NYK"])
        self.assertTrue(attestation["teamBlocksComplete"])
        self.assertEqual(
            attestation["scheduleCapturedAt"], "2026-02-10T14:58:00Z"
        )
        self.assertTrue(attestation["extractionSucceeded"])
        self.assertNotIn("complete", attestation)
        self.assertNotIn("Example, Player", json.dumps(attestation))

        parse_command, parse_options = runner.calls[1]
        self.assertEqual(parse_command[1:5], ["-layout", "-enc", "UTF-8", "-nopgbrk"])
        self.assertNotIn("SECURUS_OIDC_TOKEN", parse_options["env"])
        self.assertIs(parse_options["stdout"], subprocess.DEVNULL)

    def test_previous_day_report_verifies_current_target_date_section(self):
        previous = challenge()
        previous["target"].update({
            "reportUrl": REPORT_URL.replace("2026-02-10", "2026-02-09").replace(
                "10_00AM", "07_30PM"
            ),
            "reportDate": "2026-02-09",
            "reportPublishedAt": "2026-02-10T00:30:00Z",
            "targetGameDate": "2026-02-10",
        })
        attestation = verify_challenge(
            previous,
            pdf_opener=FakeOpener(FakeResponse(PDF_BYTES, url=previous["target"]["reportUrl"])),
            runner=FakePdftotext(
                extracted_text(
                    header_date="02/09/26",
                    header_hour="7:30",
                    header_meridiem="PM",
                )
            ),
        )
        self.assertIsNotNone(attestation)
        assert attestation is not None
        self.assertEqual(attestation["reportDate"], "2026-02-09")
        self.assertEqual(attestation["targetGameDate"], "2026-02-10")
        self.assertTrue(attestation["targetDateSectionFound"])
        self.assertEqual(attestation["targetMatchupCount"], 1)

    def test_digest_mismatch_never_invokes_pdf_parser(self):
        runner = FakePdftotext(extracted_text())
        with self.assertRaises(AttestationError):
            verify_challenge(
                challenge(),
                pdf_opener=FakeOpener(FakeResponse(PDF_BYTES[:-1] + b"x")),
                runner=runner,
            )
        self.assertEqual([call[0] for call in runner.calls], [["pdftotext", "-v"]])

    def test_parser_failure_emits_no_postable_attestation(self):
        with self.assertRaises(AttestationError):
            verify_challenge(
                challenge(),
                pdf_opener=FakeOpener(FakeResponse(PDF_BYTES)),
                runner=FakePdftotext("", extract_returncode=1),
            )

    def test_verifier_refuses_any_oidc_capability(self):
        assert_credential_free_environment({})
        for name in (
            "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
            "ACTIONS_ID_TOKEN_REQUEST_URL",
            "SECURUS_OIDC_TOKEN",
        ):
            with self.subTest(name=name):
                with self.assertRaises(AttestationError):
                    assert_credential_free_environment({name: "present"})


class SubmissionTests(unittest.TestCase):
    def _attestation(self):
        result = verify_challenge(
            challenge(),
            pdf_opener=FakeOpener(FakeResponse(PDF_BYTES)),
            runner=FakePdftotext(extracted_text()),
        )
        assert result is not None
        return result

    def test_submit_accepts_incomplete_evidence_without_client_decision(self):
        attestation = self._attestation()
        attestation["notYetSubmittedCount"] = 1
        calls = []

        def requester(url, token, **kwargs):
            calls.append((url, token, kwargs))
            return {
                "required": True,
                "stored": True,
                "complete": False,
                "sha256": attestation["sha256"],
                "verifierVersion": attestation["verifierVersion"],
            }

        response = submit_attestation(
            "https://securus.example", "oidc", attestation, requester=requester
        )
        self.assertFalse(response["complete"])
        payload = calls[0][2]["payload"]
        self.assertNotIn("complete", payload)
        self.assertEqual(payload["notYetSubmittedCount"], 1)

    def test_omitted_scheduled_matchup_is_persisted_as_incomplete(self):
        expected_two = challenge()
        expected_two["target"]["expectedMatchups"] = ["IND@NYK", "LAC@HOU"]
        attestation = verify_challenge(
            expected_two,
            pdf_opener=FakeOpener(FakeResponse(PDF_BYTES)),
            runner=FakePdftotext(extracted_text()),
        )
        assert attestation is not None
        self.assertEqual(attestation["reportedMatchups"], ["IND@NYK"])
        self.assertEqual(attestation["targetMatchupCount"], 1)
        self.assertTrue(attestation["teamBlocksComplete"])

        response = submit_attestation(
            "https://securus.example",
            "oidc",
            attestation,
            requester=lambda *args, **kwargs: {
                "required": True,
                "stored": True,
                "complete": False,
                "sha256": attestation["sha256"],
                "verifierVersion": attestation["verifierVersion"],
            },
        )
        self.assertFalse(response["complete"])

    def test_submit_requires_exact_complete_digest_and_verifier_echo(self):
        attestation = self._attestation()

        def accepted(*args, **kwargs):
            return {
                "required": True,
                "stored": True,
                "complete": True,
                "sha256": attestation["sha256"],
                "verifierVersion": attestation["verifierVersion"],
            }

        self.assertTrue(
            submit_attestation(
                "https://securus.example",
                "oidc",
                attestation,
                requester=accepted,
            )["complete"]
        )
        for override in (
            {"complete": False},
            {"sha256": "f" * 64},
            {"verifierVersion": "different-v1"},
        ):
            with self.subTest(override=override):
                response = accepted()
                response.update(override)
                with self.assertRaises(AttestationError):
                    submit_attestation(
                        "https://securus.example",
                        "oidc",
                        attestation,
                        requester=lambda *args, response=response, **kwargs: response,
                    )

    def test_submit_fails_if_site_does_not_explicitly_accept(self):
        with self.assertRaises(AttestationError):
            submit_attestation(
                "https://securus.example",
                "oidc",
                self._attestation(),
                requester=lambda *args, **kwargs: {
                    "required": True,
                    "stored": False,
                    "complete": False,
                },
            )

    def test_schema_rejects_extra_fields_and_inconsistent_failed_extraction(self):
        extra = self._attestation()
        extra["complete"] = True
        with self.assertRaises(AttestationError):
            validate_attestation(extra)

        inconsistent = self._attestation()
        inconsistent["extractionSucceeded"] = False
        with self.assertRaises(AttestationError):
            validate_attestation(inconsistent)


if __name__ == "__main__":
    unittest.main()

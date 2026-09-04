from datetime import datetime, timezone
from email.message import Message
import io
import json
import unittest
from urllib.error import HTTPError, URLError

from scripts.verify_freshness import freshness_failures, load_status


NOW = datetime(2026, 10, 1, 16, 0, tzinfo=timezone.utc)


def payload(records_written):
    return {
        "sources": [{
            "id": "nba-official-injuries",
            "lastRun": {
                "status": "SUCCEEDED",
                "recordsWritten": records_written,
                "completedAt": "2026-10-01T15:59:00Z",
            },
        }],
        "storage": {"capacity": {"capacityState": "NORMAL"}},
    }


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()
        return False


def http_error(code):
    return HTTPError("https://example.test/api/data-sources", code, "err", Message(), None)


class FreshnessTests(unittest.TestCase):
    def test_active_season_requires_a_verified_report_record(self):
        wanted = {"nba-official-injuries"}
        self.assertEqual(
            freshness_failures(payload(0), wanted, NOW),
            ["nba-official-injuries has no verified active-season report"],
        )
        self.assertEqual(freshness_failures(payload(1), wanted, NOW), [])


class StatusLoaderTests(unittest.TestCase):
    def test_transient_failures_are_retried_with_backoff(self):
        outcomes = [
            URLError("connection reset"),
            http_error(503),
            FakeResponse(json.dumps(payload(1)).encode("utf-8")),
        ]
        delays = []

        def opener(request, timeout):
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        loaded = load_status(
            "https://example.test/",
            attempts=3,
            opener=opener,
            sleeper=delays.append,
        )
        self.assertEqual(loaded["sources"][0]["id"], "nba-official-injuries")
        self.assertEqual(delays, [3.0, 6.0])
        self.assertEqual(outcomes, [])

    def test_client_errors_fail_immediately_without_retry(self):
        calls = []

        def opener(request, timeout):
            calls.append(request.full_url)
            raise http_error(401)

        with self.assertRaisesRegex(RuntimeError, "HTTP 401"):
            load_status("https://example.test", opener=opener, sleeper=lambda _s: None)
        self.assertEqual(calls, ["https://example.test/api/data-sources"])

    def test_persistent_outage_is_reported_after_all_attempts(self):
        def opener(request, timeout):
            raise TimeoutError("timed out")

        with self.assertRaisesRegex(RuntimeError, "unavailable after 3 attempts"):
            load_status("https://example.test", opener=opener, sleeper=lambda _s: None)


if __name__ == "__main__":
    unittest.main()

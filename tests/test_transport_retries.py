import json
import os
import unittest
from unittest.mock import Mock, patch
from urllib.error import URLError
from urllib.request import Request

from scripts import collect_kalshi, maintain_storage


class FakeResponse:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode("utf-8")
        self.headers = {"Content-Type": "application/json"}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, *_):
        return self.payload


class TransportRetryTests(unittest.TestCase):
    def test_kalshi_idempotent_ingest_retries_connection_reset(self):
        sleeps = []
        with patch.dict(os.environ, {"SECURUS_URL": "https://securus.invalid"}), \
             patch.object(collect_kalshi, "securus_oidc_token", return_value="token"), \
             patch.object(collect_kalshi.time, "sleep", side_effect=sleeps.append), \
             patch.object(
                 collect_kalshi,
                 "urlopen",
                 side_effect=[URLError("reset"), FakeResponse({"stored": 1})],
             ) as opened:
            result = collect_kalshi.post_json(
                "/api/data-ingest", {"kind": "stat", "records": [{}]}
            )
        self.assertEqual(result, {"stored": 1})
        self.assertEqual(opened.call_count, 2)
        self.assertEqual(sleeps, [1.0])

    def test_kalshi_journal_post_can_remain_single_attempt(self):
        with patch.dict(os.environ, {"SECURUS_URL": "https://securus.invalid"}), \
             patch.object(collect_kalshi, "securus_oidc_token", return_value="token"), \
             patch.object(collect_kalshi.time, "sleep") as sleeper, \
             patch.object(collect_kalshi, "urlopen", side_effect=URLError("reset")) as opened:
            with self.assertRaisesRegex(RuntimeError, "after 1 attempts"):
                collect_kalshi.post_json(
                    "/api/data-ingest",
                    {"kind": "sync-run", "records": [{}]},
                    attempts=1,
                )
        self.assertEqual(opened.call_count, 1)
        sleeper.assert_not_called()

    def test_maintenance_get_retries_transient_transport_failure(self):
        opener = Mock()
        opener.open.side_effect = [
            URLError("reset"),
            FakeResponse({"storage": {"capacityState": "NORMAL"}}),
        ]
        sleeps = []
        with patch.object(maintain_storage, "build_opener", return_value=opener):
            result = maintain_storage.read_json(
                Request("https://securus.invalid/api/data-sources"),
                sleep=sleeps.append,
            )
        self.assertEqual(result["storage"]["capacityState"], "NORMAL")
        self.assertEqual(opener.open.call_count, 2)
        self.assertEqual(sleeps, [1])

    def test_maintenance_post_keeps_single_transport_attempt(self):
        with patch.object(maintain_storage, "identity", return_value="token"), \
             patch.object(maintain_storage, "read_json", return_value={}) as reader:
            maintain_storage.request("/api/storage-maintenance", {"maxArchives": 12})
        self.assertEqual(reader.call_args.kwargs["attempts"], 1)
        self.assertEqual(reader.call_args.args[0].get_method(), "POST")


if __name__ == "__main__":
    unittest.main()

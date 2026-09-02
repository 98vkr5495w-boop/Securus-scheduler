from __future__ import annotations

import importlib.util
import pathlib
import unittest
from unittest.mock import patch
from urllib.error import HTTPError


SCRIPTS = pathlib.Path(__file__).parents[1] / "scripts"


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeResponse:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def read(self) -> bytes:
        return self.body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class FetchJsonRetryTests(unittest.TestCase):
    def check_module(self, module) -> None:
        sleeps: list[float] = []
        responses = iter([b"", b"<html>overloaded</html>", b'{"ok": true}'])

        def fake_urlopen(request, timeout=None):
            return FakeResponse(next(responses))

        with patch.object(module, "urlopen", side_effect=fake_urlopen), patch.object(
            module.time, "sleep", side_effect=sleeps.append
        ):
            self.assertEqual(module.fetch_json("https://example.test/ok"), {"ok": True})
        self.assertEqual(len(sleeps), 2)
        self.assertTrue(all(2.0 <= delay <= 15.0 for delay in sleeps))

        exhausted = iter([b"", b"", b""])
        with patch.object(
            module, "urlopen", side_effect=lambda request, timeout=None: FakeResponse(next(exhausted))
        ), patch.object(module.time, "sleep", return_value=None):
            with self.assertRaises(RuntimeError) as raised:
                module.fetch_json("https://example.test/broken", attempts=3)
        self.assertIn("malformed JSON response", str(raised.exception))

        def forbidden(request, timeout=None):
            raise HTTPError(request.full_url, 403, "Forbidden", {}, None)

        with patch.object(module, "urlopen", side_effect=forbidden), patch.object(
            module.time, "sleep", return_value=None
        ):
            with self.assertRaises(RuntimeError) as raised:
                module.fetch_json("https://example.test/forbidden")
        self.assertIn("HTTP 403", str(raised.exception))

    def test_weather_collector_retries_malformed_provider_bodies(self) -> None:
        self.check_module(load("collect_weather"))

    def test_kalshi_collector_retries_malformed_provider_bodies(self) -> None:
        self.check_module(load("collect_kalshi"))


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest
from unittest.mock import patch

from hbf_api_tools.http import ApiError, request_json, request_raw


class FakeResponse:
    def __init__(self, body: bytes):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, _limit):
        return self.body


class FakeOpener:
    def __init__(self, body):
        self.body = body
        self.request = None

    def open(self, request, timeout):
        self.request = request
        self.timeout = timeout
        return FakeResponse(self.body)


class HttpTest(unittest.TestCase):
    def test_json_request_uses_auth_and_encodes_query(self):
        opener = FakeOpener(b'{"ok": true}')
        with patch("hbf_api_tools.http.urllib.request.build_opener", return_value=opener):
            self.assertEqual(request_json("https://example.test", "/api", "private",
                                          query={"q": "a b"}), {"ok": True})
        self.assertEqual(opener.request.full_url, "https://example.test/api?q=a+b")
        self.assertEqual(opener.request.get_header("Authorization"), "Bearer private")
        self.assertEqual(opener.timeout, 20)

    def test_raw_response_is_bounded(self):
        opener = FakeOpener(b"x" * 2_000_001)
        with patch("hbf_api_tools.http.urllib.request.build_opener", return_value=opener):
            with self.assertRaisesRegex(ApiError, "too large"):
                request_raw("https://example.test", "/diff", "private", accept="text/plain")

    def test_arbitrary_url_route_is_rejected(self):
        with self.assertRaises(ValueError):
            request_json("https://example.test", "//attacker.test/path", "private")


if __name__ == "__main__":
    unittest.main()

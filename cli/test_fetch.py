import unittest
from unittest.mock import Mock, patch

import requests

from fetch import fetch_json

URL = "https://api.example.com/data"


def make_response(status=200, json_value=None, json_error=None):
    response = Mock(status_code=status)
    if json_error:
        response.json.side_effect = json_error
    else:
        response.json.return_value = json_value
    return response


@patch("fetch.requests.get")
class FetchJsonTests(unittest.TestCase):
    def test_returns_any_json_value(self, get):
        for value in ({"a": 1}, [1, 2], "text", 3, True, None):
            get.return_value = make_response(json_value=value)
            self.assertEqual(fetch_json(URL), value)

    def test_request_settings(self, get):
        get.return_value = make_response(json_value={})
        fetch_json(URL)
        get.assert_called_once_with(
            URL,
            headers={"Accept": "application/json"},
            timeout=10,
            verify=True,
            allow_redirects=False,
        )

    def test_invalid_urls_make_no_request(self, get):
        for url in ("", "   ", None, "ftp://example.com", "https://", "example.com",
                    "https://exa mple.com", "https://example.com:abc",
                    "https://example.com:99999", "https://example.com:0"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                fetch_json(url)
        get.assert_not_called()

    def test_timeout(self, get):
        get.side_effect = requests.exceptions.Timeout()
        with self.assertRaisesRegex(RuntimeError, "timed out"):
            fetch_json(URL)

    def test_connection_error(self, get):
        get.side_effect = requests.exceptions.ConnectionError("refused")
        with self.assertRaisesRegex(RuntimeError, "Network request failed"):
            fetch_json(URL)

    def test_certificate_error(self, get):
        get.side_effect = requests.exceptions.SSLError()
        with self.assertRaisesRegex(RuntimeError, "certificate"):
            fetch_json(URL)

    def test_unsuccessful_status(self, get):
        for status in (301, 404, 500):
            get.return_value = make_response(status=status)
            with self.subTest(status=status), self.assertRaisesRegex(RuntimeError, str(status)):
                fetch_json(URL)
            get.return_value.json.assert_not_called()

    def test_invalid_json(self, get):
        get.return_value = make_response(json_error=requests.exceptions.JSONDecodeError("bad", "", 0))
        with self.assertRaisesRegex(ValueError, "not valid JSON"):
            fetch_json(URL)


if __name__ == "__main__":
    unittest.main()

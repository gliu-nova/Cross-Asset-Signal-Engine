"""Tests for fetch helpers."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import pandas as pd
import requests

from src.fetch import FetchError, _fred_latest, _yahoo_latest, fred_api_status, fred_response_json


class YahooLatestTests(unittest.TestCase):
    @patch("src.fetch.yf.Ticker")
    def test_uses_last_valid_close_when_latest_row_is_nan(self, mock_ticker_cls: MagicMock) -> None:
        hist = pd.DataFrame(
            {
                "Close": [100.0, float("nan")],
            },
            index=pd.to_datetime(["2026-06-25", "2026-06-26"]),
        )
        mock_ticker_cls.return_value.history.return_value = hist

        value, observed = _yahoo_latest("QQQ")

        self.assertEqual(value, 100.0)
        self.assertEqual(observed, "2026-06-25")

    @patch("src.fetch.yf.Ticker")
    def test_raises_when_all_closes_are_nan(self, mock_ticker_cls: MagicMock) -> None:
        hist = pd.DataFrame(
            {"Close": [float("nan"), float("nan")]},
            index=pd.to_datetime(["2026-06-25", "2026-06-26"]),
        )
        mock_ticker_cls.return_value.history.return_value = hist

        with self.assertRaises(FetchError):
            _yahoo_latest("QQQ")


def _response(body: str, content_type: str, payload: dict | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.text = body
    resp.headers = {"Content-Type": content_type}
    if payload is None:
        resp.json.side_effect = requests.exceptions.JSONDecodeError("Expecting value", body, 0)
    else:
        resp.json.return_value = payload
    return resp


class FredJsonTests(unittest.TestCase):
    def test_html_maintenance_page_is_fetch_error(self) -> None:
        resp = _response(
            "<!doctype html><h1>We'll be back soon!</h1>",
            "text/html; charset=utf-8",
        )

        with self.assertRaises(FetchError) as caught:
            fred_response_json(resp, "SP500")

        message = str(caught.exception)
        self.assertIn("status 200", message)
        self.assertIn("text/html", message)
        self.assertIn("We'll be back soon!", message)

    def test_json_object_parses(self) -> None:
        payload = {"observations": [{"date": "2026-10-21", "value": "5812.3"}]}
        resp = _response('{"observations":[]}', "application/json", payload)

        self.assertEqual(fred_response_json(resp, "SP500"), payload)

    @patch("src.fetch.time.sleep")
    @patch("src.fetch._http_get")
    @patch.dict("os.environ", {"FRED_API_KEY": "test-key"})
    def test_non_json_retries_once_then_raises(self, mock_get: MagicMock, mock_sleep: MagicMock) -> None:
        mock_get.return_value = _response("<html>maintenance</html>", "text/html")

        with self.assertRaises(FetchError):
            _fred_latest("SP500")

        self.assertEqual(mock_get.call_count, 2)
        mock_sleep.assert_called_once_with(1.0)

    @patch("src.fetch.time.sleep")
    @patch("src.fetch._http_get")
    @patch.dict("os.environ", {"FRED_API_KEY": "test-key"})
    def test_retry_uses_json_after_html(self, mock_get: MagicMock, mock_sleep: MagicMock) -> None:
        payload = {"observations": [{"date": "2026-10-21", "value": "5812.3"}]}
        mock_get.side_effect = [
            _response("<html>maintenance</html>", "text/html"),
            _response("{}", "application/json", payload),
        ]

        value, observed = _fred_latest("SP500")

        self.assertEqual(value, 5812.3)
        self.assertEqual(observed, "2026-10-21")
        mock_sleep.assert_called_once_with(1.0)

    @patch("src.fetch.requests.get")
    def test_health_rejects_html_body(self, mock_get: MagicMock) -> None:
        mock_get.return_value = _response(
            "<!doctype html><h1>We'll be back soon!</h1>",
            "text/html; charset=utf-8",
        )
        mock_get.return_value.raise_for_status.return_value = None

        with self.assertRaises(FetchError) as caught:
            fred_api_status("test-key")

        self.assertIn("We'll be back soon!", str(caught.exception))

    @patch("src.fetch.requests.get")
    def test_health_accepts_observations_json(self, mock_get: MagicMock) -> None:
        payload = {"observations": [{"date": "2026-10-21", "value": "4.1"}]}
        mock_get.return_value = _response("{}", "application/json", payload)
        mock_get.return_value.raise_for_status.return_value = None

        fred_api_status("test-key")


if __name__ == "__main__":
    unittest.main()

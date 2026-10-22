"""Tests for reading quality checks."""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import requests

from src.fetch import FetchError
from src.quality import QualityError, cross_verify


class CrossVerifyTests(unittest.TestCase):
    def test_request_exception_skips_check(self) -> None:
        err = requests.exceptions.JSONDecodeError("Expecting value", "", 0)
        buf = io.StringIO()
        with patch("src.quality.fetch_indicator", side_effect=err):
            with redirect_stdout(buf):
                cross_verify(100.0, {"source": "fred", "series": "SP500"}, 3.0, "S&P 500")

        self.assertIn("cross-verify skipped", buf.getvalue())

    def test_fetch_error_skips_check(self) -> None:
        buf = io.StringIO()
        with patch("src.quality.fetch_indicator", side_effect=FetchError("FRED SP500 returned non-JSON")):
            with redirect_stdout(buf):
                cross_verify(100.0, {"source": "fred", "series": "SP500"}, 3.0, "S&P 500")

        self.assertIn("cross-verify skipped", buf.getvalue())

    def test_mismatch_still_raises(self) -> None:
        with patch("src.quality.fetch_indicator", return_value=(200.0, "2026-10-21")):
            with self.assertRaises(QualityError):
                cross_verify(100.0, {"source": "fred", "series": "SP500"}, 3.0, "S&P 500")


if __name__ == "__main__":
    unittest.main()

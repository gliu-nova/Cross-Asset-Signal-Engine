"""Spot tracking for HYPE, XRP, LINK, TAO, and ZEC."""

from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from src.alerts import check_alert
from src.config import indicator_settings, load_config
from src.fetch import fetch_chart_history, fetch_indicator
from src.market_memory_bridge import PRICE_DIRECTION_INDICATORS
from src.posting.compose import _format_value
from src.posting.decide import decide_tweet_type
from src.posting.models import CATEGORY_GROUPS, AlertTrigger
from src.posting.scoring import calculate_score, effective_score
from src.scheduler import CRYPTO_KEYS, poll_schedule


ALT_KEYS = ("hype", "xrp", "link", "tao", "zec")


def _memory_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE readings (
            indicator TEXT NOT NULL,
            value REAL NOT NULL,
            observed_at TEXT NOT NULL,
            recorded_at TEXT NOT NULL
        );
        CREATE TABLE alert_log (
            indicator TEXT PRIMARY KEY,
            last_value REAL,
            last_alert_at TEXT
        );
        """
    )
    return conn


def _seed(conn: sqlite3.Connection, key: str, value: float) -> None:
    conn.execute(
        "INSERT INTO readings (indicator, value, observed_at, recorded_at) VALUES (?, ?, ?, ?)",
        (key, value, "2026-06-01", "2026-06-01T00:00:00+00:00"),
    )


class AltCryptoConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cfg = load_config()

    def test_indicators_are_tracked_on_the_crypto_schedule(self) -> None:
        themes = self.cfg["posting"]["indicator_themes"]
        for key in ALT_KEYS:
            self.assertIn(key, self.cfg["indicators"], key)
            self.assertIn(key, themes, key)
            self.assertEqual(themes[key]["category"], "crypto")
            self.assertIn(key, CRYPTO_KEYS, key)
            self.assertIn(key, CATEGORY_GROUPS["crypto"], key)
            self.assertIn(key, PRICE_DIRECTION_INDICATORS, key)
            settings = indicator_settings(self.cfg, key)
            self.assertEqual(poll_schedule(settings), "crypto_24_7")
            self.assertEqual(settings["quality"]["schedule"], "crypto_24_7")
            thresholds = [
                rule["threshold"]
                for rule in settings["rules"]
                if rule["type"] == "percent_change"
            ]
            self.assertEqual(thresholds, [settings["normal_alert"]])

    def test_sources_match_listings(self) -> None:
        hype = indicator_settings(self.cfg, "hype")
        self.assertEqual(hype["source"], "kraken")
        self.assertEqual(hype["pair"], "HYPEUSD")
        self.assertEqual(hype["quality"]["verify"]["source"], "coingecko")
        self.assertEqual(hype["quality"]["verify"]["coin_id"], "hyperliquid")

        tao = indicator_settings(self.cfg, "tao")
        self.assertEqual(tao["symbol"], "TAO22974-USD")
        self.assertEqual(tao["quality"]["verify"]["pair"], "TAOUSD")

        for key, symbol, pair in (
            ("xrp", "XRP-USD", "XRPUSD"),
            ("link", "LINK-USD", "LINKUSD"),
            ("zec", "ZEC-USD", "ZECUSD"),
        ):
            settings = indicator_settings(self.cfg, key)
            self.assertEqual(settings["source"], "yahoo")
            self.assertEqual(settings["symbol"], symbol)
            self.assertEqual(settings["quality"]["verify"]["pair"], pair)


class AltCryptoAlertTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cfg = load_config()
        cls.posting = cls.cfg["posting"]

    def _move(self, key: str, prev: float, value: float):
        conn = _memory_db()
        try:
            _seed(conn, key, prev)
            settings = indicator_settings(self.cfg, key)
            return check_alert(conn, settings, value), settings
        finally:
            conn.close()

    def test_move_inside_normal_threshold_does_not_alert(self) -> None:
        for key in ALT_KEYS:
            settings = indicator_settings(self.cfg, key)
            (fired, alert), _settings = self._move(
                key, 100.0, 100.0 * (1 + (settings["normal_alert"] - 0.1) / 100)
            )
            self.assertFalse(fired, key)
            self.assertIsNone(alert)

    def test_normal_move_alerts_but_does_not_standalone_post(self) -> None:
        fired_pair, settings = self._move("xrp", 100.0, 106.0)
        fired, alert = fired_pair
        self.assertTrue(fired)
        self.assertEqual(alert.alert_tier, "normal")
        alert.score = calculate_score(alert, settings, self.posting)
        alert.score = effective_score(alert, settings, self.posting, in_session=True)
        self.assertIsNone(decide_tweet_type([alert], self.posting))

    def test_emergency_move_posts_during_the_us_session(self) -> None:
        high_single = float(self.posting["high_single_threshold"])
        for key in ALT_KEYS:
            settings = indicator_settings(self.cfg, key)
            jump = float(settings["emergency_alert"])
            fired, alert = self._move(key, 100.0, 100.0 + jump)[0]
            self.assertTrue(fired, key)
            self.assertEqual(alert.alert_tier, "emergency", key)
            alert.score = calculate_score(alert, settings, self.posting)
            alert.score = effective_score(alert, settings, self.posting, in_session=True)
            self.assertGreaterEqual(alert.score, high_single, key)
            decision = decide_tweet_type([alert], self.posting)
            self.assertIsNotNone(decision, key)
            self.assertEqual(decision.tweet_type, "single")
            self.assertTrue(decision.is_emergency)


class AltCryptoFetchTests(unittest.TestCase):
    @patch("src.fetch._kraken_latest", return_value=(67.22, "2026-06-01 00:00"))
    def test_fetch_hype_uses_kraken(self, mock_kraken: unittest.mock.MagicMock) -> None:
        cfg = load_config()
        value, observed = fetch_indicator(indicator_settings(cfg, "hype"))
        self.assertEqual(value, 67.22)
        self.assertEqual(observed, "2026-06-01 00:00")
        mock_kraken.assert_called_once_with("HYPEUSD")

    @patch("src.fetch._yahoo_latest", return_value=(0.95, "2026-06-01"))
    def test_fetch_xrp_uses_yahoo(self, mock_yahoo: unittest.mock.MagicMock) -> None:
        cfg = load_config()
        value, _observed = fetch_indicator(indicator_settings(cfg, "xrp"))
        self.assertEqual(value, 0.95)
        mock_yahoo.assert_called_once_with("XRP-USD")

    @patch("src.fetch._http_get")
    def test_kraken_chart_history_uses_daily_closes(self, mock_get: MagicMock) -> None:
        now = datetime.now(timezone.utc)
        recent = int((now - timedelta(days=1)).timestamp())
        old = int((now - timedelta(days=400)).timestamp())
        body = {
            "error": [],
            "result": {
                "HYPEUSD": [
                    [old, "1", "2", "0.5", "1.5", "1", "10", 1],
                    [recent, "60", "70", "55", "67.2", "66", "10", 1],
                ],
                "last": recent,
            },
        }
        mock_get.return_value.json.return_value = body
        rows = fetch_chart_history({"source": "kraken", "pair": "HYPEUSD"}, months=6)
        self.assertEqual(rows, [(datetime.fromtimestamp(recent, timezone.utc).strftime("%Y-%m-%d"), 67.2)])


class AltCryptoFormatTests(unittest.TestCase):
    def test_price_labels(self) -> None:
        now = datetime.now(timezone.utc)
        samples = {
            "xrp": (0.9521, "$0.9521"),
            "link": (9.14, "$9.14"),
            "hype": (67.22, "$67.22"),
            "tao": (189.5, "$189.5"),
            "zec": (889.7, "$889.7"),
        }
        for key, (value, expected) in samples.items():
            alert = AlertTrigger(
                indicator=key,
                name=key,
                value=value,
                prev_value=None,
                reasons=[],
                rule_types=[],
                themes=["crypto"],
                category="crypto",
                is_macro=False,
                timestamp=now,
            )
            self.assertEqual(_format_value(alert), expected, key)

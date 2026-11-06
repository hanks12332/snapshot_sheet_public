import unittest
from datetime import date
from unittest.mock import Mock, patch

import fetchers


class FetchersTests(unittest.TestCase):
    def test_fetch_crypto_falls_back_to_kraken(self):
        with patch.object(fetchers, "_fetch_binance", return_value=None) as binance:
            with patch.object(fetchers, "_fetch_kraken", return_value=123.45) as kraken:
                self.assertEqual(fetchers.fetch_crypto("bitcoin"), 123.45)
        binance.assert_called_once_with("bitcoin")
        kraken.assert_called_once_with("bitcoin")

    def test_sina_quote_batch_parses_each_requested_symbol(self):
        response = Mock()
        response.text = (
            'var hq_str_nf_I2601="iron,2026-01-01,0,0,0,0,0,0,800.5";'
            'var hq_str_nf_I2605="iron,2026-01-01,0,0,0,0,0,0,810.5";'
        )
        with patch.object(fetchers._session, "get", return_value=response) as get:
            result = fetchers._sina_quote_batch(
                ["nf_I2601", "nf_I2605"], attempts=1
            )

        self.assertEqual(result["nf_I2601"][8], "800.5")
        self.assertEqual(result["nf_I2605"][8], "810.5")
        get.assert_called_once()

    def test_fetch_iron_ore_contracts_selects_highest_open_interest(self):
        candidates = ["I2601", "I2605", "I2609"]

        def fields(price, open_interest):
            values = [""] * 15
            values[8] = str(price)
            values[14] = str(open_interest)
            return values

        quotes = {
            "nf_I2601": fields(700, 100),
            "nf_I2605": fields(710, 300),
            "nf_I2609": fields(720, 200),
        }
        with patch.object(fetchers, "_iron_ore_candidates", return_value=candidates):
            with patch.object(fetchers, "_sina_quote_batch", return_value=quotes):
                result = fetchers.fetch_iron_ore_contracts()

        self.assertEqual(
            result,
            {
                "main_code": "I2605",
                "main_price": 710.0,
                "next_code": "I2609",
                "next_price": 720.0,
            },
        )

    def test_sina_futures_returns_none_for_short_quote(self):
        with patch.object(fetchers, "_sina_quote", return_value=["name"]):
            self.assertIsNone(fetchers.fetch_sina_futures("I0"))

    def test_us_etf_uses_keyless_nasdaq_quote(self):
        with patch.object(fetchers, "_fetch_nasdaq_etf", return_value=704.78) as nasdaq:
            with patch.object(fetchers, "_fetch_yahoo_chart") as yahoo:
                self.assertEqual(fetchers.fetch_sina_us_stock("VOO"), 704.78)

        nasdaq.assert_called_once_with("VOO")
        yahoo.assert_not_called()

    def test_nasdaq_trade_date_reads_both_timestamp_forms(self):
        self.assertEqual(fetchers._nasdaq_trade_date("Nov 4, 2026"), date(2026, 11, 4))
        self.assertEqual(fetchers._nasdaq_trade_date("Nov 5, 2026 3:59 PM ET"), date(2026, 11, 5))
        self.assertIsNone(fetchers._nasdaq_trade_date(None))
        self.assertIsNone(fetchers._nasdaq_trade_date("N/A"))

    @staticmethod
    def _nasdaq_response(price, stamp):
        response = Mock()
        response.json.return_value = {
            "data": {"primaryData": {"lastSalePrice": price, "lastTradeTimestamp": stamp}}
        }
        return response

    def test_nasdaq_etf_uses_a_quote_dated_today(self):
        response = self._nasdaq_response("$704.7078", "Nov 5, 2026 3:59 PM ET")
        with patch.object(fetchers._session, "get", return_value=response), patch.object(
            fetchers, "_new_york_today", return_value=date(2026, 11, 5)
        ):
            self.assertEqual(fetchers._fetch_nasdaq_etf("VOO"), 704.7078)

    def test_nasdaq_etf_ignores_an_earlier_days_close(self):
        # 2026-11-05 ~21:00 ET: the endpoint served the Nov 4 close while Nov 5 had closed 4.4 % lower
        response = self._nasdaq_response("$704.78", "Nov 4, 2026")
        with patch.object(fetchers._session, "get", return_value=response), patch.object(
            fetchers, "_new_york_today", return_value=date(2026, 11, 5)
        ):
            self.assertIsNone(fetchers._fetch_nasdaq_etf("VOO"))

    @staticmethod
    def _nasdaq_chart_response(points):
        response = Mock()
        response.json.return_value = {"data": {"chart": points}}
        return response

    def test_nasdaq_chart_uses_the_last_priced_point_of_the_latest_session(self):
        # Nov 5, 2026 session: x is the New York wall-clock time encoded as UTC epoch milliseconds
        points = [
            {"x": 1793908740000, "y": 675.1, "z": {"dateTime": "7:59 PM ET", "value": "675.10"}},
            {"x": 1793908800000, "y": 673.97, "z": {"dateTime": "8:00 PM ET", "value": "673.97"}},
            {"x": 1793908860000, "y": None, "z": {}},
        ]
        for today in (date(2026, 11, 5), date(2026, 11, 8)):  # the evening after, and the Sunday
            with patch.object(fetchers._session, "get", return_value=self._nasdaq_chart_response(points)), patch.object(
                fetchers, "_new_york_today", return_value=today
            ):
                self.assertEqual(fetchers._fetch_nasdaq_chart("VOO"), 673.97)

    def test_nasdaq_chart_ignores_a_session_older_than_the_limit(self):
        points = [{"x": 1793908800000, "y": 673.97, "z": {"dateTime": "8:00 PM ET"}}]
        with patch.object(fetchers._session, "get", return_value=self._nasdaq_chart_response(points)), patch.object(
            fetchers, "_new_york_today", return_value=date(2026, 11, 12)
        ):
            self.assertIsNone(fetchers._fetch_nasdaq_chart("VOO"))
        with patch.object(fetchers._session, "get", return_value=self._nasdaq_chart_response([])):
            self.assertIsNone(fetchers._fetch_nasdaq_chart("VOO"))

    def test_us_etf_falls_back_to_the_nasdaq_chart_when_the_quote_is_stale(self):
        with patch.object(fetchers, "_fetch_nasdaq_etf", return_value=None), patch.object(
            fetchers, "_fetch_nasdaq_chart", return_value=673.97
        ) as chart, patch.object(fetchers, "_fetch_yahoo_chart") as yahoo:
            self.assertEqual(fetchers.fetch_sina_us_stock("VOO"), 673.97)

        chart.assert_called_once_with("VOO")
        yahoo.assert_not_called()

    def test_us_etf_falls_back_to_yahoo_when_both_nasdaq_sources_fail(self):
        with patch.object(fetchers, "_fetch_nasdaq_etf", return_value=None), patch.object(
            fetchers, "_fetch_nasdaq_chart", return_value=None
        ), patch.object(fetchers, "_fetch_yahoo_chart", return_value=673.97) as yahoo:
            self.assertEqual(fetchers.fetch_sina_us_stock("VOO"), 673.97)

        yahoo.assert_called_once_with("VOO")

    def test_fx_uses_keyless_yahoo_chart(self):
        with patch.object(fetchers, "_fetch_yahoo_chart", return_value=6.667) as yahoo:
            self.assertEqual(fetchers.fetch_sina_fx("USDCNH"), 6.667)

        yahoo.assert_called_once_with("USDCNH=X")

    def test_fx_falls_back_to_fresh_keyless_biquote(self):
        with patch.object(fetchers, "_fetch_yahoo_chart", return_value=None), patch.object(
            fetchers, "_fetch_biquote", return_value=6.6665
        ) as biquote:
            self.assertEqual(fetchers.fetch_sina_fx("USDCNH"), 6.6665)

        biquote.assert_called_once_with("USDCNH")

    def test_biquote_uses_mid_quote(self):
        response = Mock()
        response.json.return_value = {
            "mid": 6.66601,
            "stale": False,
            "quoteAgeSeconds": 4,
        }
        with patch.object(fetchers._session, "get", return_value=response):
            self.assertEqual(fetchers._fetch_biquote("USDCNH"), 6.66601)

    def test_biquote_uses_bid_ask_when_mid_missing(self):
        response = Mock()
        response.json.return_value = {
            "bid": 6.66476,
            "ask": 6.66726,
            "stale": False,
            "quoteAgeSeconds": 0,
        }
        with patch.object(fetchers._session, "get", return_value=response):
            self.assertAlmostEqual(fetchers._fetch_biquote("USDCNH"), 6.66601)

    def test_biquote_rejects_stale_or_old_quote(self):
        for payload in (
            {"mid": 6.666, "stale": True, "quoteAgeSeconds": 0},
            {"mid": 6.666, "stale": False, "quoteAgeSeconds": 301},
        ):
            response = Mock()
            response.json.return_value = payload
            with patch.object(fetchers._session, "get", return_value=response):
                self.assertIsNone(fetchers._fetch_biquote("USDCNH"))

    def test_gold_uses_keyless_tencent_quote(self):
        with patch.object(fetchers, "_fetch_tencent_spot", return_value=4118.88) as tencent:
            with patch.object(fetchers, "_fetch_gold_api") as gold:
                self.assertEqual(fetchers.fetch_sina_spot("hf_XAU"), 4118.88)

        tencent.assert_called_once_with("hf_XAU")
        gold.assert_not_called()


if __name__ == "__main__":
    unittest.main()

import unittest
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


if __name__ == "__main__":
    unittest.main()

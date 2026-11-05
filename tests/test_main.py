import re
import sys
import unittest
from unittest.mock import patch

import main


class MainTests(unittest.TestCase):
    def test_build_row_has_expected_columns(self):
        with patch.object(main.fetchers, "fetch_iron_ore_contracts", return_value={
            "main_code": "I2605",
            "main_price": 710.0,
            "next_code": "I2609",
            "next_price": 720.0,
        }), patch.object(main.fetchers, "fetch_crypto", side_effect=[100.0, 200.0]), patch.object(
            main.fetchers, "fetch_sina_us_stock", side_effect=[300.0, 400.0]
        ), patch.object(main.fetchers, "fetch_sina_fx", return_value=7.2), patch.object(
            main.fetchers, "fetch_sina_spot", return_value=2300.0
        ), patch.object(main.fetchers, "fetch_treasury", return_value=99.0), patch.object(
            main.fetchers, "fetch_sina_futures", side_effect=[800.0, 900.0]
        ):
            row = main.build_row()

        self.assertEqual(len(row), len(main.feishu.HEADER_ROW))
        self.assertRegex(row[0], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
        self.assertEqual(row[1:], [100.0, 200.0, 300.0, 400.0, 7.2, 2300.0, 99.0, 691, 3121, "I2701", "I2705", 681])

    def test_dry_run_does_not_write_to_feishu(self):
        row = ["timestamp"] + [1.0] * (len(main.feishu.HEADER_ROW) - 1)
        with patch.object(main, "build_row", return_value=row), patch.object(
            main, "setup_logging"
        ), patch.object(main, "load_dotenv"), patch.object(
            main.feishu, "prepend_row"
        ) as prepend, patch.object(sys, "argv", ["main.py", "--dry-run"]):
            self.assertEqual(main.main(), 0)

        prepend.assert_not_called()

    def test_missing_value_skips_sheet_write(self):
        row = ["timestamp", None] + [1.0] * (len(main.feishu.HEADER_ROW) - 2)
        with patch.object(main, "build_row", return_value=row), patch.object(
            main, "setup_logging"
        ), patch.object(main, "load_dotenv"), patch.object(
            main.feishu, "prepend_row"
        ) as prepend, patch.object(sys, "argv", ["main.py"]):
            self.assertEqual(main.main(), 0)

        prepend.assert_not_called()

    def test_dce_columns_use_reference_values(self):
        with patch.object(
            main.fetchers, "fetch_crypto", side_effect=[100.0, 200.0]
        ), patch.object(
            main.fetchers, "fetch_sina_us_stock", side_effect=[300.0, 400.0]
        ), patch.object(
            main.fetchers, "fetch_sina_fx", return_value=7.2
        ), patch.object(
            main.fetchers, "fetch_sina_spot", return_value=2300.0
        ), patch.object(
            main.fetchers, "fetch_treasury", return_value=99.0
        ):
            row = main.build_row()

        self.assertEqual(row[8:13], [691, 3121, "I2701", "I2705", 681])


if __name__ == "__main__":
    unittest.main()

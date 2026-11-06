import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import feishu


def response(payload, status_code=200):
    result = Mock()
    result.status_code = status_code
    result.ok = 200 <= status_code < 400
    result.text = json.dumps(payload)
    result.json.return_value = payload
    return result


class FeishuTests(unittest.TestCase):
    def test_load_cached_token_ignores_expired_token(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "token.json"
            cache.write_text(json.dumps({"token": "old", "expires_at": 0}))
            with patch.object(feishu, "TOKEN_CACHE", cache):
                self.assertIsNone(feishu._load_cached_token())

    def test_trim_rows_deletes_rows_after_cap(self):
        metainfo = response(
            {
                "code": 0,
                "data": {"sheets": [{"sheetId": "sheet-1", "rowCount": 1002}]},
            }
        )
        delete = response({"code": 0})
        with patch.object(feishu.requests, "get", return_value=metainfo):
            with patch.object(feishu.requests, "delete", return_value=delete) as request:
                feishu._trim_rows("spreadsheet", "sheet-1", "token")

        body = request.call_args.kwargs["json"]
        self.assertEqual(body["dimension"]["startIndex"], 1002)
        self.assertEqual(body["dimension"]["endIndex"], 1002)

    def test_prepend_row_writes_header_and_appends(self):
        with tempfile.TemporaryDirectory() as directory:
            sentinel = Path(directory) / ".header_written"
            backup = Path(directory) / "failed_rows.csv"
            trim_info = response(
                {
                    "code": 0,
                    "data": {"sheets": [{"sheetId": "sheet-1", "rowCount": 1}]},
                }
            )
            with patch.object(feishu, "HEADER_SENTINEL", sentinel), patch.object(
                feishu, "BACKUP_CSV", backup
            ), patch.object(
                feishu, "_config", return_value=("app", "secret", "spreadsheet", "sheet-1")
            ), patch.object(feishu, "get_tenant_token", return_value="token"), patch.object(
                feishu.requests, "put", return_value=response({"code": 0})
            ) as put, patch.object(
                feishu.requests, "post", return_value=response({"code": 0})
            ) as post, patch.object(
                feishu.requests, "get", return_value=trim_info
            ):
                feishu.prepend_row(["timestamp", 1.0])
            self.assertTrue(sentinel.exists())
        self.assertIn(f"A1:{feishu.LAST_COL}1", put.call_args.kwargs["json"]["valueRange"]["range"])
        self.assertIn(f"A2:{feishu.LAST_COL}2", post.call_args.kwargs["json"]["valueRange"]["range"])
        self.assertFalse(backup.exists())

    def test_prepend_row_backs_up_failed_write(self):
        with tempfile.TemporaryDirectory() as directory:
            sentinel = Path(directory) / ".header_written"
            sentinel.touch()
            backup = Path(directory) / "failed_rows.csv"
            with patch.object(feishu, "HEADER_SENTINEL", sentinel), patch.object(
                feishu, "BACKUP_CSV", backup
            ), patch.object(
                feishu, "_config", return_value=("app", "secret", "spreadsheet", "sheet-1")
            ), patch.object(feishu, "get_tenant_token", return_value="token"), patch.object(
                feishu.requests, "post", side_effect=RuntimeError("offline")
            ):
                with self.assertRaises(RuntimeError):
                    feishu.prepend_row(["timestamp", 1.0])

            self.assertEqual(backup.read_text().strip(), "timestamp,1.0")


if __name__ == "__main__":
    unittest.main()

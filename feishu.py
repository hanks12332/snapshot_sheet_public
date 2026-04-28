"""Feishu (Lark) spreadsheet client: token cache + append_row."""

from __future__ import annotations

import csv
import json
import logging
import os
import time
from pathlib import Path
from typing import List

import requests

log = logging.getLogger(__name__)

BASE = "https://open.feishu.cn/open-apis"
TOKEN_CACHE = Path("/tmp/feishu_token.json")
BACKUP_CSV = Path(__file__).parent / "failed_rows.csv"
HEADER_SENTINEL = Path(__file__).parent / ".header_written"
TIMEOUT = 10
MAX_DATA_ROWS = 1000  # header row + this many snapshots; older rows are trimmed

HEADER_ROW = [
    "Timestamp",
    "BTC",
    "ETH",
    "VOO",
    "BOXX",
    "USDCNH",
    "GOLD (spot XAU/USD)",
    "91282CMM0",
    "I0.DCE",
    "M0.DCE",
    "I0 main contract",
    "I0 next contract",
    "I0 next price",
]
LAST_COL = "M"  # columns A..M = 13 fields


def _config():
    app_id = os.environ["FEISHU_APP_ID"]
    app_secret = os.environ["FEISHU_APP_SECRET"]
    sheet_token = os.environ["FEISHU_SHEET_TOKEN"]
    sheet_id = os.environ.get("FEISHU_SHEET_ID") or None
    return app_id, app_secret, sheet_token, sheet_id


def _load_cached_token() -> str | None:
    try:
        data = json.loads(TOKEN_CACHE.read_text())
        if data.get("expires_at", 0) > time.time() + 60:
            return data["token"]
    except (FileNotFoundError, ValueError, KeyError):
        pass
    return None


def _save_cached_token(token: str, ttl: int) -> None:
    TOKEN_CACHE.write_text(json.dumps({"token": token, "expires_at": time.time() + ttl}))


def get_tenant_token(force_refresh: bool = False) -> str:
    if not force_refresh:
        cached = _load_cached_token()
        if cached:
            return cached
    app_id, app_secret, _, _ = _config()
    r = requests.post(
        f"{BASE}/auth/v3/tenant_access_token/internal",
        json={"app_id": app_id, "app_secret": app_secret},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    data = r.json()
    if data.get("code") != 0:
        raise RuntimeError(f"tenant_access_token error: {data}")
    _save_cached_token(data["tenant_access_token"], int(data.get("expire", 7200)))
    return data["tenant_access_token"]


def discover_sheet_id(sheet_token: str, token: str) -> str:
    r = requests.get(
        f"{BASE}/sheets/v2/spreadsheets/{sheet_token}/metainfo",
        headers={"Authorization": f"Bearer {token}"},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    data = r.json()
    if data.get("code") != 0:
        raise RuntimeError(f"metainfo error: {data}")
    sheets = data["data"]["sheets"]
    first = sheets[0]
    return first["sheetId"]


def _backup_row(values: List) -> None:
    BACKUP_CSV.parent.mkdir(parents=True, exist_ok=True)
    with BACKUP_CSV.open("a", newline="") as f:
        csv.writer(f).writerow(values)


def _trim_rows(sheet_token: str, sheet_id: str, token: str) -> None:
    """Delete grid rows past row (1 + MAX_DATA_ROWS). No-op if sheet is still small."""
    cap = 1 + MAX_DATA_ROWS
    r = requests.get(
        f"{BASE}/sheets/v2/spreadsheets/{sheet_token}/metainfo",
        headers={"Authorization": f"Bearer {token}"},
        timeout=TIMEOUT,
    )
    if not r.ok:
        raise RuntimeError(f"metainfo HTTP {r.status_code}: {r.text[:300]}")
    data = r.json()
    if data.get("code") != 0:
        raise RuntimeError(f"metainfo error: {data}")
    target = next((s for s in data["data"]["sheets"] if s["sheetId"] == sheet_id), None)
    if target is None:
        return
    row_count = target["rowCount"]
    if row_count <= cap:
        return
    r = requests.delete(
        f"{BASE}/sheets/v2/spreadsheets/{sheet_token}/dimension_range",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={
            "dimension": {
                "sheetId": sheet_id,
                "majorDimension": "ROWS",
                "startIndex": cap + 1,
                "endIndex": row_count,
            }
        },
        timeout=TIMEOUT,
    )
    if not r.ok:
        raise RuntimeError(f"dimension_range delete HTTP {r.status_code}: {r.text[:300]}")
    data = r.json()
    if data.get("code") != 0:
        raise RuntimeError(f"dimension_range delete error: {data}")
    log.info("trimmed %d rows (grid was %d, capped to %d)", row_count - cap, row_count, cap)


def _write_header(sheet_token: str, sheet_id: str, token: str) -> None:
    """Overwrite row 1 with HEADER_ROW. Idempotent; safe to call once per install."""
    r = requests.put(
        f"{BASE}/sheets/v2/spreadsheets/{sheet_token}/values",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"valueRange": {"range": f"{sheet_id}!A1:{LAST_COL}1", "values": [HEADER_ROW]}},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    data = r.json()
    if data.get("code") != 0:
        raise RuntimeError(f"header write error: {data}")
    HEADER_SENTINEL.touch()
    log.info("header row written")


def prepend_row(values: List) -> None:
    """Insert a fresh row at position 2 so newest snapshots sit at the top, under the header."""
    app_id, app_secret, sheet_token, sheet_id = _config()
    token = get_tenant_token()

    if not sheet_id:
        sheet_id = discover_sheet_id(sheet_token, token)
        log.info("discovered sheet_id=%s — save to .env to skip next time", sheet_id)

    if not HEADER_SENTINEL.exists():
        _write_header(sheet_token, sheet_id, token)

    url = f"{BASE}/sheets/v2/spreadsheets/{sheet_token}/values_prepend"
    body = {"valueRange": {"range": f"{sheet_id}!A2:{LAST_COL}2", "values": [values]}}

    def _do(tok: str):
        return requests.post(
            url,
            params={"valueInputOption": "USER_ENTERED"},
            headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
            json=body,
            timeout=TIMEOUT,
        )

    try:
        r = _do(token)
        if r.status_code == 401 or (r.ok and r.json().get("code") == 99991663):
            log.info("token rejected, refreshing once")
            token = get_tenant_token(force_refresh=True)
            r = _do(token)
        if not r.ok:
            raise RuntimeError(f"values_prepend HTTP {r.status_code}: {r.text[:500]}")
        data = r.json()
        if data.get("code") != 0:
            raise RuntimeError(f"values_prepend error: {data}")
        log.info("prepended row: %s", values)
    except Exception:
        _backup_row(values)
        raise

    try:
        _trim_rows(sheet_token, sheet_id, token)
    except Exception as e:
        log.warning("row trim failed (non-fatal): %s", e)

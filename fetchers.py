"""Price fetchers. Each returns float|None; errors are logged, never raised.

All live-quote sources below are reachable from mainland China, so yfinance
(Yahoo) is intentionally not used — it is blocked there.
"""

from __future__ import annotations

import logging
import re
from datetime import date
from typing import Optional

import requests

log = logging.getLogger(__name__)

TIMEOUT = 10
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
SINA_HEADERS = {"Referer": "https://finance.sina.com.cn", "User-Agent": UA}

_session = requests.Session()


def _safe(name: str):
    def deco(fn):
        def wrapper(*args, **kwargs):
            try:
                v = fn(*args, **kwargs)
                if v is None:
                    log.warning("%s returned None", name)
                return v
            except Exception as e:
                log.warning("%s failed: %s", name, e)
                return None
        wrapper.__name__ = fn.__name__
        return wrapper
    return deco


def _sina_quote_batch(symbols: list[str], attempts: int = 3) -> dict[str, list[str]]:
    """Batch-fetch multiple Sina quotes in one HTTP call. Returns {symbol: fields}."""
    if not symbols:
        return {}
    joined = ",".join(symbols)
    last_exc: Exception | None = None
    for _ in range(attempts):
        try:
            r = _session.get(
                f"https://hq.sinajs.cn/list={joined}",
                headers=SINA_HEADERS,
                timeout=TIMEOUT,
            )
            r.encoding = "gbk"
            result: dict[str, list[str]] = {}
            for sym in symbols:
                m = re.search(rf'var hq_str_{re.escape(sym)}="([^"]*)"', r.text)
                if m and m.group(1):
                    result[sym] = m.group(1).split(",")
            return result
        except requests.exceptions.RequestException as e:
            last_exc = e
            continue
    if last_exc:
        raise last_exc
    return {}


def _sina_quote(symbol: str, attempts: int = 3) -> list[str]:
    """GET hq.sinajs.cn/list=<symbol> and return the CSV fields inside the quotes."""
    last_exc: Exception | None = None
    for _ in range(attempts):
        try:
            r = _session.get(
                f"https://hq.sinajs.cn/list={symbol}",
                headers=SINA_HEADERS,
                timeout=TIMEOUT,
            )
            r.encoding = "gbk"
            m = re.search(r'"([^"]*)"', r.text)
            if m and m.group(1):
                return m.group(1).split(",")
            return []
        except requests.exceptions.RequestException as e:
            last_exc = e
            continue
    if last_exc:
        raise last_exc
    return []


@_safe("coingecko")
def fetch_coingecko(coin_id: str) -> Optional[float]:
    r = _session.get(
        "https://api.coingecko.com/api/v3/simple/price",
        params={"ids": coin_id, "vs_currencies": "usd"},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return float(r.json()[coin_id]["usd"])


@_safe("sina_us_stock")
def fetch_sina_us_stock(ticker: str) -> Optional[float]:
    """US stocks via Sina: `gb_<lowercase ticker>`. Field [1] is latest price."""
    fields = _sina_quote(f"gb_{ticker.lower()}")
    if len(fields) < 2:
        return None
    return float(fields[1])


@_safe("sina_fx")
def fetch_sina_fx(pair: str) -> Optional[float]:
    """FX via Sina: `fx_s<lowercase pair>`. Field [1] is latest price."""
    fields = _sina_quote(f"fx_s{pair.lower()}")
    if len(fields) < 2:
        return None
    return float(fields[1])


@_safe("sina_spot")
def fetch_sina_spot(symbol: str) -> Optional[float]:
    """Spot commodity via Sina: e.g. `hf_XAU`. Field [0] is latest price."""
    fields = _sina_quote(symbol)
    if not fields:
        return None
    return float(fields[0])


@_safe("sina_futures")
def fetch_sina_futures(symbol: str) -> Optional[float]:
    """DCE main-continuous via Sina's `nf_` (domestic futures) endpoint.

    The bare `I0`/`M0` endpoint returns stale 2024 data — use `nf_` for live quotes.
    Field layout: [0]name [1]time [2]open [3]high [4]low [5]prev_close
                  [6]bid1 [7]ask1 [8]last [9]settle [10]prev_settle ...
    """
    fields = _sina_quote(f"nf_{symbol}")
    if len(fields) < 9:
        return None
    return float(fields[8])


IRON_ORE_ROTATION = [1, 5, 9]  # DCE iron ore listed months


def _iron_ore_candidates() -> list[str]:
    """Plausible active contract codes around today, in chronological order."""
    today = date.today()
    codes: list[str] = []
    for year_offset in range(3):
        yy = (today.year + year_offset) % 100
        for m in IRON_ORE_ROTATION:
            codes.append(f"I{yy:02d}{m:02d}")
    return codes


@_safe("iron_ore_contracts")
def fetch_iron_ore_contracts() -> Optional[dict]:
    """Return {'main_code','main_price','next_code','next_price'}.

    Main = candidate with highest open interest (field [14]).
    Next = the candidate immediately after main in chronological listing order.
    """
    codes = _iron_ore_candidates()
    quotes = _sina_quote_batch([f"nf_{c}" for c in codes])
    live: list[dict] = []
    for code in codes:
        fields = quotes.get(f"nf_{code}")
        if not fields or len(fields) < 15:
            continue
        try:
            last = float(fields[8])
            oi = float(fields[14])
        except (ValueError, TypeError):
            continue
        if last <= 0 or oi <= 0:
            continue
        live.append({"code": code, "last": last, "oi": oi})
    if len(live) < 2:
        return None
    main_idx = max(range(len(live)), key=lambda i: live[i]["oi"])
    if main_idx + 1 >= len(live):
        return None
    main, nxt = live[main_idx], live[main_idx + 1]
    return {
        "main_code": main["code"],
        "main_price": main["last"],
        "next_code": nxt["code"],
        "next_price": nxt["last"],
    }


@_safe("treasury_webull")
def fetch_treasury(cusip: str) -> Optional[float]:
    """Latest price for a specific US Treasury by CUSIP via Webull's public quote page."""
    r = _session.get(
        f"https://www.webull.com/quote/bond-{cusip.lower()}",
        headers={"User-Agent": UA},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    m = re.search(r'"price"\s*:\s*"?([0-9.]+)"?', r.text)
    if m:
        return float(m.group(1))
    m = re.search(r'data-testid="last-price"[^>]*>([0-9.]+)<', r.text)
    if m:
        return float(m.group(1))
    return None

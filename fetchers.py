"""Price fetchers. Each returns float|None; errors are logged, never raised.

The primary sources below are public HTTP endpoints and do not require API
keys. They are still subject to rate limits and can change without notice, so
each market has a fallback where practical.
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


# Binance symbol map for unauthenticated public ticker (no API key required)
_BINANCE_SYMBOLS: dict[str, str] = {
    "bitcoin": "BTCUSDT",
    "ethereum": "ETHUSDT",
}

# Kraken pair map used as fallback
_KRAKEN_PAIRS: dict[str, str] = {
    "bitcoin": "XBTUSD",
    "ethereum": "ETHUSD",
}

_YAHOO_CHART_HOSTS = ("query2.finance.yahoo.com", "query1.finance.yahoo.com")


def _fetch_yahoo_chart(symbol: str) -> Optional[float]:
    """Read a latest quote from Yahoo's public chart endpoint.

    This endpoint is unauthenticated, but may return HTTP 429. Try both public
    hosts before giving up so callers can fall back to another provider.
    """
    for host in _YAHOO_CHART_HOSTS:
        try:
            r = _session.get(
                f"https://{host}/v8/finance/chart/{symbol}",
                params={"range": "1d", "interval": "1d"},
                headers={"User-Agent": UA},
                timeout=TIMEOUT,
            )
            r.raise_for_status()
            result = (r.json().get("chart") or {}).get("result") or []
            if not result:
                continue
            meta = result[0].get("meta") or {}
            value = meta.get("regularMarketPrice")
            if value is None:
                quote = ((result[0].get("indicators") or {}).get("quote") or [{}])[0]
                closes = quote.get("close") or []
                value = next((v for v in reversed(closes) if v is not None), None)
            if value is not None:
                return float(value)
        except (requests.RequestException, ValueError, TypeError, KeyError) as exc:
            log.debug("yahoo chart %s failed on %s: %s", symbol, host, exc)
    return None


@_safe("nasdaq_etf")
def _fetch_nasdaq_etf(ticker: str) -> Optional[float]:
    """Read an ETF's current sale price from Nasdaq's public quote endpoint."""
    r = _session.get(
        f"https://api.nasdaq.com/api/quote/{ticker.upper()}/info",
        params={"assetclass": "etf"},
        headers={
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://www.nasdaq.com",
            "Referer": "https://www.nasdaq.com/",
            "User-Agent": UA,
        },
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    data = (r.json().get("data") or {}).get("primaryData") or {}
    raw = data.get("lastSalePrice")
    if raw is None:
        return None
    return float(re.sub(r"[^0-9.+-]", "", str(raw)))


@_safe("tencent_spot")
def _fetch_tencent_spot(symbol: str) -> Optional[float]:
    """Read a Tencent quote such as ``hf_XAU`` without an API key."""
    r = _session.get(
        "https://qt.gtimg.cn/q=" + symbol,
        headers={"User-Agent": UA},
        timeout=TIMEOUT,
    )
    r.encoding = "gbk"
    match = re.search(rf'v_{re.escape(symbol)}="([^"]*)"', r.text)
    if not match or not match.group(1):
        return None
    return float(match.group(1).split(",")[0])


@_safe("gold_api")
def _fetch_gold_api() -> Optional[float]:
    """Read XAU/USD from the public gold-api.com endpoint."""
    r = _session.get("https://api.gold-api.com/price/XAU", timeout=TIMEOUT)
    r.raise_for_status()
    value = (r.json() or {}).get("price")
    return float(value) if value is not None else None


@_safe("currency_api")
def _fetch_currency_api(pair: str) -> Optional[float]:
    """Read a daily currency-api.dev rate as a no-key FX fallback."""
    pair = pair.upper()
    if len(pair) != 6:
        return None
    base, quote = pair[:3].lower(), pair[3:].lower()
    r = _session.get(
        f"https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@latest/v1/currencies/{base}.json",
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    value = (r.json().get(base) or {}).get(quote)
    return float(value) if value is not None else None


@_safe("binance")
def _fetch_binance(coin_id: str) -> Optional[float]:
    symbol = _BINANCE_SYMBOLS.get(coin_id)
    if not symbol:
        return None
    r = _session.get(
        "https://api.binance.com/api/v3/ticker/price",
        params={"symbol": symbol},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return float(r.json()["price"])


@_safe("kraken")
def _fetch_kraken(coin_id: str) -> Optional[float]:
    pair = _KRAKEN_PAIRS.get(coin_id)
    if not pair:
        return None
    r = _session.get(
        "https://api.kraken.com/0/public/Ticker",
        params={"pair": pair},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    data = r.json()
    if data.get("error"):
        return None
    for v in data.get("result", {}).values():
        return float(v["c"][0])  # "c" = last trade closed price
    return None


def fetch_crypto(coin_id: str) -> Optional[float]:
    """Fetch a crypto spot price in USD, trying Binance then Kraken.

    coin_id must be a CoinGecko-style name: 'bitcoin' or 'ethereum'.
    Both sources are unauthenticated public endpoints; no API key needed.
    """
    price = _fetch_binance(coin_id)
    if price is not None:
        return price
    log.warning("crypto | binance failed for %s, falling back to kraken", coin_id)
    return _fetch_kraken(coin_id)


@_safe("sina_us_stock")
def fetch_sina_us_stock(ticker: str) -> Optional[float]:
    """US ETF via Nasdaq, Yahoo, then the legacy Sina endpoint."""
    price = _fetch_nasdaq_etf(ticker)
    if price is not None:
        return price
    price = _fetch_yahoo_chart(ticker.upper())
    if price is not None:
        return price
    fields = _sina_quote(f"gb_{ticker.lower()}")
    if len(fields) < 2:
        return None
    return float(fields[1])


@_safe("sina_fx")
def fetch_sina_fx(pair: str) -> Optional[float]:
    """FX via Yahoo's public chart endpoint, then Sina."""
    yahoo_symbol = {"USDCNH": "USDCNH=X"}.get(pair.upper(), f"{pair.upper()}=X")
    price = _fetch_yahoo_chart(yahoo_symbol)
    if price is not None:
        return price
    price = _fetch_currency_api(pair)
    if price is not None:
        return price
    fields = _sina_quote(f"fx_s{pair.lower()}")
    if len(fields) < 2:
        return None
    return float(fields[1])


@_safe("sina_spot")
def fetch_sina_spot(symbol: str) -> Optional[float]:
    """Spot gold via Tencent, gold-api.com, then Sina."""
    if symbol.upper() == "HF_XAU":
        price = _fetch_tencent_spot("hf_XAU")
        if price is not None:
            return price
        price = _fetch_gold_api()
        if price is not None:
            return price
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
    m = re.search(r'"preClose"\s*:\s*"([0-9]+\.[0-9]+)"', r.text)
    if m:
        return float(m.group(1))
    return None

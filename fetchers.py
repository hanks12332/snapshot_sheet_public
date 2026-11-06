"""Price fetchers. Each returns float|None; errors are logged, never raised.

The primary sources below are public HTTP endpoints and do not require API
keys. They are still subject to rate limits and can change without notice, so
each market has a fallback where practical.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import socket
import ssl
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import requests

log = logging.getLogger(__name__)

TIMEOUT = 10
NASDAQ_TIMEOUT = 25  # api.nasdaq.com answers slowly at times (2026-11-06 09:00 UTC: 16-20 s or nothing)
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
SINA_HEADERS = {"Referer": "https://finance.sina.com.cn", "User-Agent": UA}

_session = requests.Session()
_US_EASTERN = ZoneInfo("America/New_York")
NASDAQ_CHART_MAX_AGE_DAYS = 4  # the chart endpoint shows the latest session: a weekend plus a holiday at most


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

# Biquote publishes broker quotes without an API key. Five minutes is long
# enough to tolerate a transient request delay while preventing a stale quote
# from being written to the daily snapshot.
BIQUOTE_MAX_AGE_SECONDS = 300


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


def _new_york_today() -> date:
    return datetime.now(_US_EASTERN).date()


def _nasdaq_trade_date(stamp) -> Optional[date]:
    """The date of a Nasdaq ``lastTradeTimestamp`` such as ``"Nov 4, 2026"`` or
    ``"Nov 5, 2026 3:59 PM ET"``; None when it is missing or unreadable."""
    match = re.match(r"\s*([A-Za-z]{3})\.?\s+(\d{1,2}),\s*(\d{4})", str(stamp or ""))
    if not match:
        return None
    try:
        return datetime.strptime(" ".join(match.groups()), "%b %d %Y").date()
    except ValueError:
        return None


# --- Binance Stocks (US equities): keyless WebSocket quote stream, best bid / ask, 24/5 ---------------------
# wss://nbstream.binance.com/equity/ws/<SYMBOL>@quote pushes {"e": "quote", "s": "VOO", "bp": bid, "ap": ask,
# "bs"/"as": sizes, "T": quote time ms, "src": venue} without an API key (the REST quote needs one). Overnight
# quotes come from the Blue Ocean session (src BLUE); a closed market pushes nothing.
BINANCE_EQUITY_WS_HOST = "nbstream.binance.com"
BINANCE_EQUITY_SYMBOLS = ("VOO", "BOXX")  # fetched together, one session per run
BINANCE_EQUITY_WAIT_S = 15.0  # BOXX quotes are sparse (a few per minute overnight)
BINANCE_EQUITY_MAX_AGE_S = 600  # a quote stamped older than this is not used
BINANCE_EQUITY_MAX_SPREAD = 0.02  # ask / bid - 1 beyond this: no usable mid

_equity_quotes_cache: Optional[dict] = None

# Last good price per ticker (fetchers.py's directory, JSON {ticker: {"price": x, "ts": epoch s}}). A ticker in
# CARRY_FORWARD_S reuses it, for at most that many seconds, when every source fails: BOXX is a cash-like ETF
# that moves about 0.01 a day and has almost no quotes outside the US session.
LAST_PRICES_FILE = Path(__file__).with_name(".last_prices.json")
CARRY_FORWARD_S = {"BOXX": 24 * 3600}


def _load_last_prices() -> dict:
    try:
        return json.loads(LAST_PRICES_FILE.read_text())
    except (OSError, ValueError):
        return {}


def _remember_price(ticker: str, price: float, now: Optional[float] = None) -> None:
    data = _load_last_prices()
    data[ticker.upper()] = {"price": price, "ts": time.time() if now is None else now}
    try:
        tmp = LAST_PRICES_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(LAST_PRICES_FILE)
    except OSError as exc:
        log.debug("could not save last prices: %s", exc)


def _carried_price(ticker: str, now: Optional[float] = None) -> Optional[float]:
    """The last good price of `ticker` when it may be carried forward and is young enough, else None."""
    max_age = CARRY_FORWARD_S.get(ticker.upper())
    if max_age is None:
        return None
    entry = _load_last_prices().get(ticker.upper())
    if not entry:
        return None
    try:
        price, stamp = float(entry["price"]), float(entry["ts"])
    except (KeyError, TypeError, ValueError):
        return None
    age = (time.time() if now is None else now) - stamp
    if not 0 <= age <= max_age:
        return None
    log.warning("%s %s carried forward from %.1f h ago: every source failed", ticker.upper(), price, age / 3600)
    return price


def _ws_decode(buf: bytes):
    """Split a WebSocket byte buffer into ([(fin, opcode, payload)], remaining bytes). Handles the 7-bit, 16-bit
    and 64-bit length forms and masked payloads; an incomplete trailing frame stays in the remainder."""
    frames = []
    while len(buf) >= 2:
        first, second = buf[0], buf[1]
        fin, opcode = bool(first & 0x80), first & 0x0F
        masked, length = bool(second & 0x80), second & 0x7F
        i = 2
        if length == 126:
            if len(buf) < 4:
                break
            length, i = int.from_bytes(buf[2:4], "big"), 4
        elif length == 127:
            if len(buf) < 10:
                break
            length, i = int.from_bytes(buf[2:10], "big"), 10
        mask = b""
        if masked:
            if len(buf) < i + 4:
                break
            mask, i = buf[i:i + 4], i + 4
        if len(buf) < i + length:
            break
        payload = buf[i:i + length]
        if mask:
            payload = bytes(b ^ mask[k % 4] for k, b in enumerate(payload))
        frames.append((fin, opcode, payload))
        buf = buf[i + length:]
    return frames, buf


def _ws_client_frame(opcode: int, payload: bytes = b"") -> bytes:
    """One masked client frame (the protocol requires client-to-server masking)."""
    mask = os.urandom(4)
    n = len(payload)
    head = bytes([0x80 | opcode])
    if n < 126:
        head += bytes([0x80 | n])
    elif n < 65536:
        head += bytes([0x80 | 126]) + n.to_bytes(2, "big")
    else:
        head += bytes([0x80 | 127]) + n.to_bytes(8, "big")
    return head + mask + bytes(b ^ mask[k % 4] for k, b in enumerate(payload))


def _equity_quote_session(symbols, wait_s: float) -> dict:
    """One session on Binance's equity quote streams: {symbol: latest quote event} received within wait_s,
    returning as soon as every symbol has one. Keyless; the socket is closed afterwards."""
    path = "/equity/stream?streams=" + "/".join(f"{sym}@quote" for sym in symbols)
    key = base64.b64encode(os.urandom(16)).decode()
    deadline = time.monotonic() + wait_s
    sock = ssl.create_default_context().wrap_socket(
        socket.create_connection((BINANCE_EQUITY_WS_HOST, 443), timeout=TIMEOUT),
        server_hostname=BINANCE_EQUITY_WS_HOST,
    )
    quotes: dict = {}
    try:
        sock.sendall(
            (
                f"GET {path} HTTP/1.1\r\nHost: {BINANCE_EQUITY_WS_HOST}\r\nUpgrade: websocket\r\n"
                f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
                f"User-Agent: {UA}\r\n\r\n"
            ).encode()
        )
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = sock.recv(4096)
            if not chunk:
                raise RuntimeError("connection closed during the handshake")
            response += chunk
        head, _, buf = response.partition(b"\r\n\r\n")
        status = head.split(b"\r\n", 1)[0].decode(errors="replace")
        if " 101 " not in status:
            raise RuntimeError(f"handshake: {status}")
        wanted = set(symbols)
        partial = b""
        while wanted - set(quotes) and time.monotonic() < deadline:
            sock.settimeout(max(0.1, deadline - time.monotonic()))
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                break
            buf += chunk
            frames, buf = _ws_decode(buf)
            for fin, opcode, payload in frames:
                if opcode == 9:  # ping
                    sock.sendall(_ws_client_frame(10, payload))
                elif opcode == 8:  # server close
                    wanted = set()
                    break
                elif opcode in (0, 1):
                    partial += payload
                    if not fin:
                        continue
                    message, partial = partial, b""
                    try:
                        event = json.loads(message)
                    except ValueError:
                        continue
                    if isinstance(event, dict):
                        event = event.get("data", event)
                    if isinstance(event, dict) and event.get("e") == "quote" and event.get("s") in wanted:
                        quotes[event["s"]] = event
        try:
            sock.sendall(_ws_client_frame(8, b"\x03\xe8"))
        except OSError:
            pass
    finally:
        sock.close()
    return quotes


def _equity_mid(event: dict, now_ms: int) -> Optional[float]:
    """The mid of a quote event's best bid / ask; None when the quote is unreadable, crossed, wider than
    BINANCE_EQUITY_MAX_SPREAD or stamped more than BINANCE_EQUITY_MAX_AGE_S ago."""
    try:
        bid, ask = float(event["bp"]), float(event["ap"])
        stamp = int(event.get("T") or event["E"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (bid > 0 and ask >= bid) or ask / bid - 1 > BINANCE_EQUITY_MAX_SPREAD:
        return None
    if now_ms - stamp > BINANCE_EQUITY_MAX_AGE_S * 1000:
        return None
    return round((bid + ask) / 2, 3)


@_safe("binance_equity")
def _fetch_binance_equity(ticker: str) -> Optional[float]:
    """Mid of the latest best bid / ask on Binance's US-equity quote stream (24/5, keyless). The tickers in
    BINANCE_EQUITY_SYMBOLS are fetched together in one session per run (the first call opens it, later calls read
    the result); a market that pushes nothing within BINANCE_EQUITY_WAIT_S gives None and the caller moves on."""
    global _equity_quotes_cache
    ticker = ticker.upper()
    if _equity_quotes_cache is None or ticker not in _equity_quotes_cache:
        symbols = BINANCE_EQUITY_SYMBOLS if ticker in BINANCE_EQUITY_SYMBOLS else (ticker,)
        got = _equity_quote_session(symbols, BINANCE_EQUITY_WAIT_S)
        cache = dict(_equity_quotes_cache or {})
        cache.update({sym: got.get(sym) for sym in symbols})
        _equity_quotes_cache = cache
    event = _equity_quotes_cache.get(ticker)
    if not event:
        log.info("binance_equity %s: no quote within %.0f s (market closed?); trying the next source", ticker,
                 BINANCE_EQUITY_WAIT_S)
        return None
    mid = _equity_mid(event, int(time.time() * 1000))
    if mid is None:
        log.info("binance_equity %s: quote not usable (%s); trying the next source", ticker, event)
        return None
    log.info("binance_equity %s: bid %s ask %s src %s T %s", ticker, event.get("bp"), event.get("ap"),
             event.get("src"), event.get("T"))
    return mid


@_safe("nasdaq_etf")
def _fetch_nasdaq_etf(ticker: str) -> Optional[float]:
    """Read an ETF's current sale price from Nasdaq's public quote endpoint.

    Outside the regular session the endpoint can serve an earlier day's close
    under that day's ``lastTradeTimestamp`` (2026-11-05 ~21:00 ET: VOO showed
    the Nov 4 close, 704.78, while Nov 5 had closed at 673.97). Only a quote
    dated today in New York is used; otherwise the caller moves on to Yahoo.
    """
    r = _session.get(
        f"https://api.nasdaq.com/api/quote/{ticker.upper()}/info",
        params={"assetclass": "etf"},
        headers={
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://www.nasdaq.com",
            "Referer": "https://www.nasdaq.com/",
            "User-Agent": UA,
        },
        timeout=NASDAQ_TIMEOUT,
    )
    r.raise_for_status()
    data = (r.json().get("data") or {}).get("primaryData") or {}
    raw = data.get("lastSalePrice")
    if raw is None:
        return None
    stamp = data.get("lastTradeTimestamp")
    today = _new_york_today()
    if _nasdaq_trade_date(stamp) != today:
        log.info("nasdaq_etf %s: quote dated %r is not from today (%s); trying the next source", ticker, stamp, today)
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


@_safe("biquote")
def _fetch_biquote(pair: str) -> Optional[float]:
    """Read a fresh no-key FX quote from Biquote.

    Biquote returns ``mid`` plus bid/ask and freshness metadata. A daily
    exchange-rate feed is deliberately not used here: the snapshot needs an
    intraday USDCNH quote, and a daily value can be hours away from the market.
    """
    pair = pair.upper()
    if len(pair) != 6:
        return None

    r = _session.get(
        f"https://biquote.io/api/{pair}",
        headers={"Accept": "application/json", "User-Agent": UA},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    payload = r.json()
    if not isinstance(payload, dict):
        return None

    stale = payload.get("stale")
    if stale is True or str(stale).lower() == "true":
        return None

    age = payload.get("quoteAgeSeconds")
    if age is not None:
        try:
            age_seconds = float(age)
        except (TypeError, ValueError):
            return None
        if age_seconds < 0 or age_seconds > BIQUOTE_MAX_AGE_SECONDS:
            return None
    else:
        # Older response formats may omit quoteAgeSeconds. In that case use a
        # parseable timestamp; without either signal freshness cannot be
        # established safely.
        timestamp = payload.get("lastQuoteAt") or payload.get("timestamp")
        if not timestamp:
            return None
        try:
            quoted_at = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
            if quoted_at.tzinfo is None:
                quoted_at = quoted_at.replace(tzinfo=timezone.utc)
            age_seconds = (datetime.now(timezone.utc) - quoted_at).total_seconds()
        except (TypeError, ValueError):
            return None
        if age_seconds < 0 or age_seconds > BIQUOTE_MAX_AGE_SECONDS:
            return None

    value = payload.get("mid")
    if value is None:
        try:
            bid = float(payload["bid"])
            ask = float(payload["ask"])
        except (KeyError, TypeError, ValueError):
            return None
        value = (bid + ask) / 2
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


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


@_safe("nasdaq_chart")
def _fetch_nasdaq_chart(ticker: str) -> Optional[float]:
    """The last price of the latest session on Nasdaq's chart endpoint, extended hours included.

    Unlike the info endpoint it kept serving the day's session after the nightly
    reset (2026-11-05 21:17 ET: Nov 5 points up to 8:00 PM ET while the info quote
    showed Nov 4). Point times are New York wall-clock times encoded as UTC epoch
    milliseconds; a session older than NASDAQ_CHART_MAX_AGE_DAYS is not used.
    """
    r = _session.get(
        f"https://api.nasdaq.com/api/quote/{ticker.upper()}/chart",
        params={"assetclass": "etf"},
        headers={
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://www.nasdaq.com",
            "Referer": "https://www.nasdaq.com/",
            "User-Agent": UA,
        },
        timeout=NASDAQ_TIMEOUT,
    )
    r.raise_for_status()
    points = (r.json().get("data") or {}).get("chart") or []
    last = next((pt for pt in reversed(points) if pt.get("y") is not None and pt.get("x") is not None), None)
    if last is None:
        return None
    traded = datetime.fromtimestamp(int(last["x"]) / 1000, tz=timezone.utc).date()
    today = _new_york_today()
    if not -1 <= (today - traded).days <= NASDAQ_CHART_MAX_AGE_DAYS:
        log.info("nasdaq_chart %s: latest session %s is too old for today (%s)", ticker, traded, today)
        return None
    log.info("nasdaq_chart %s: %s at %s %s", ticker, last["y"], traded, (last.get("z") or {}).get("dateTime"))
    return float(last["y"])


@_safe("sina_us_stock")
def fetch_sina_us_stock(ticker: str) -> Optional[float]:
    """US ETF: Binance's equity quote stream (24/5 mid), then Nasdaq's quote (today's only), Nasdaq's chart (the
    latest session), Yahoo, then Sina; a fresh price is remembered, and a ticker in CARRY_FORWARD_S reuses its last
    good price when every source fails."""
    price = _fetch_us_etf(ticker)
    if price is not None:
        _remember_price(ticker, price)
        return price
    return _carried_price(ticker)


def _fetch_us_etf(ticker: str) -> Optional[float]:
    price = _fetch_binance_equity(ticker)
    if price is not None:
        log.info("%s %s via binance_equity", ticker, price)
        return price
    price = _fetch_nasdaq_etf(ticker)
    if price is not None:
        log.info("%s %s via nasdaq", ticker, price)
        return price
    price = _fetch_nasdaq_chart(ticker)
    if price is not None:
        log.info("%s %s via nasdaq_chart", ticker, price)
        return price
    price = _fetch_yahoo_chart(ticker.upper())
    if price is not None:
        log.info("%s %s via yahoo", ticker, price)
        return price
    fields = _sina_quote(f"gb_{ticker.lower()}")
    if len(fields) < 2:
        return None
    log.info("%s %s via sina", ticker, fields[1])
    return float(fields[1])


@_safe("sina_fx")
def fetch_sina_fx(pair: str) -> Optional[float]:
    """FX via Yahoo, fresh Biquote, then the legacy Sina endpoint.

    The Biquote fallback is intraday and keyless. Daily currency feeds are
    intentionally excluded because they can produce a materially stale value.
    """
    yahoo_symbol = {"USDCNH": "USDCNH=X"}.get(pair.upper(), f"{pair.upper()}=X")
    price = _fetch_yahoo_chart(yahoo_symbol)
    if price is not None:
        return price
    price = _fetch_biquote(pair)
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

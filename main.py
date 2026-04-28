"""Orchestrator: fetch 8 asset prices, append as a row to the Feishu sheet."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path

from dotenv import load_dotenv

import feishu
import fetchers

ROOT = Path(__file__).parent
LOG_FILE = ROOT / "snapshot_sheet.log"

SHANGHAI = timezone(timedelta(hours=8))


def setup_logging(verbose: bool) -> None:
    handlers = [
        RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=3),
        logging.StreamHandler(sys.stdout),
    ]
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
        handlers=handlers,
    )


def build_row() -> list:
    io = fetchers.fetch_iron_ore_contracts()
    return [
        datetime.now(SHANGHAI).strftime("%Y-%m-%d %H:%M:%S"),
        fetchers.fetch_coingecko("bitcoin"),
        fetchers.fetch_coingecko("ethereum"),
        fetchers.fetch_sina_us_stock("VOO"),
        fetchers.fetch_sina_us_stock("BOXX"),
        fetchers.fetch_sina_fx("USDCNH"),
        fetchers.fetch_sina_spot("hf_XAU"),
        fetchers.fetch_treasury("91282CMM0"),
        fetchers.fetch_sina_futures("I0"),
        fetchers.fetch_sina_futures("M0"),
        io["main_code"] if io else None,
        io["next_code"] if io else None,
        io["next_price"] if io else None,
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="fetch prices but skip Feishu append")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    setup_logging(args.verbose)
    log = logging.getLogger("main")

    try:
        row = build_row()
        log.info("row: %s", row)
        missing = [feishu.HEADER_ROW[i] for i, v in enumerate(row) if v is None]
        if missing:
            log.warning("skipping sheet write — %d asset(s) missing: %s", len(missing), missing)
            return 0
        if args.dry_run:
            print("dry-run:", row)
            return 0
        feishu.prepend_row(row)
        return 0
    except Exception:
        log.exception("run failed")
        return 0  # always exit 0 so launchd doesn't throttle


if __name__ == "__main__":
    sys.exit(main())
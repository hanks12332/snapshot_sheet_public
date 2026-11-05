# Snapshot Sheet

This job collects BTC, ETH, VOO, BOXX, USDCNH, spot gold, a US Treasury quote,
and DCE futures prices, then prepends one row to a Feishu spreadsheet.

## Requirements

- Python 3.9 or newer (CI uses Python 3.12)
- Network access to the public Binance/Kraken, Yahoo/Biquote, Sina, and Webull quote endpoints
- A Feishu custom app with spreadsheet read/write permission
- A Feishu spreadsheet that the app can edit

Copy `.env.example` to `.env` and fill in:

```text
FEISHU_APP_ID=cli_...
FEISHU_APP_SECRET=...
FEISHU_SHEET_TOKEN=sht...
FEISHU_SHEET_ID=            # optional; the first sheet is discovered when empty
```

The app must be granted access to the spreadsheet. `FEISHU_SHEET_ID` is the
sheet identifier from the spreadsheet URL; leaving it blank makes the program
discover the first sheet through the Feishu API.

## Local setup

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
python main.py --dry-run
python -m unittest discover -s tests -v
python main.py
```

`--dry-run` still fetches market data but does not write to Feishu. It is useful
for checking network access and parsing without configuring spreadsheet access.
`run.sh` expects the virtual environment at `.venv`; the launchd plist contains
`REPO_PATH` placeholders that must be replaced with the absolute checkout path.
If DCE futures data is unavailable, the I0/M0 and iron-ore contract columns are
written as `0`; other missing assets still prevent a sheet write.

The default fallbacks do not require API keys: Nasdaq public ETF quotes for
VOO/BOXX, Yahoo's chart endpoint followed by Biquote for USDCNH, and Tencent's
quote endpoint followed by gold-api.com for XAU/USD. The Biquote quote is
accepted only when it is marked fresh and no more than five minutes old. These
public endpoints have no SLA; Yahoo may return HTTP 429, in which case the
intraday Biquote quote is used when available. Daily exchange-rate feeds are
not used for the snapshot because they can be stale during market hours.

## Automation

The GitHub Actions workflow is manual (`workflow_dispatch`) and expects the four
`FEISHU_*` values as repository secrets. It runs the unit tests before the live
snapshot job. On macOS, load `local.snapshot_sheet.plist` after replacing its
`REPO_PATH` values.

If a write fails, the row is appended to `failed_rows.csv` for later recovery.

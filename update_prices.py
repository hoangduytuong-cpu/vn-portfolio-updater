"""
update_prices.py
────────────────
Fetches the latest closing prices for Vietnamese stocks via yfinance
and writes them into a Google Sheet.

Sheet convention (configurable via SHEET_CONFIG below):
  Column A : ticker symbols as you type them (e.g. VIC, LPB, HPG)
  Column B : last closing price  (written by this script)
  Column C : date of that price  (written by this script)
  Row 1    : headers — skipped automatically

Set TICKER_COL / PRICE_COL / DATE_COL to match your actual sheet layout.
"""

import os
import json
import time
import logging
from datetime import datetime, timedelta, timezone

import yfinance as yf
import gspread
from google.oauth2.service_account import Credentials

# ── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Sheet layout config — change these to match YOUR sheet ─────────────────
SHEET_CONFIG = {
    "spreadsheet_id": os.environ["SPREADSHEET_ID"],   # set in GitHub Secret
    "worksheet_name": "Portfolio",                     # tab name in your sheet
    "header_rows": 1,                                  # rows to skip at top
    "ticker_col": 0,   # 0-indexed: column A = 0
    "price_col": 1,    # 0-indexed: column B = 1
    "date_col": 2,     # 0-indexed: column C = 2
}

# Vietnam timezone offset (ICT = UTC+7)
ICT = timezone(timedelta(hours=7))

# ── Google Sheets auth ─────────────────────────────────────────────────────
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]

def get_gspread_client() -> gspread.Client:
    """
    Authenticate using the service-account JSON stored as a GitHub Secret.
    The secret value is the full JSON content (minified to one line).
    """
    creds_json = os.environ["GOOGLE_CREDENTIALS"]
    creds_dict = json.loads(creds_json)
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    return gspread.authorize(creds)


# ── Price fetching ─────────────────────────────────────────────────────────
def yf_ticker(symbol: str) -> str:
    """
    Convert a plain Vietnamese ticker (e.g. 'VIC') to Yahoo Finance format.
    HOSE & HNX stocks both use the .VN suffix on Yahoo Finance.
    UPCoM stocks are also reachable this way in most cases.
    """
    return f"{symbol.strip().upper()}.VN"


def fetch_closing_prices(tickers: list[str]) -> dict[str, tuple[float | None, str | None]]:
    """
    Download the most recent available closing price for each ticker.

    Returns a dict:  { 'VIC': (price_float, 'YYYY-MM-DD'), ... }
    Missing / failed tickers get (None, None).
    """
    yf_tickers = [yf_ticker(t) for t in tickers]
    results: dict[str, tuple[float | None, str | None]] = {}

    # Download 5 days of daily data for all tickers in one API call
    # (5 days ensures we catch the last close even around holidays)
    try:
        raw = yf.download(
            tickers=yf_tickers,
            period="5d",
            interval="1d",
            auto_adjust=True,
            progress=False,
            group_by="ticker",
        )
    except Exception as exc:
        log.error("yfinance download failed: %s", exc)
        return {t: (None, None) for t in tickers}

    for original, yf_sym in zip(tickers, yf_tickers):
        try:
            if len(yf_tickers) == 1:
                # Single-ticker download has a flat column structure
                close_series = raw["Close"]
            else:
                close_series = raw[yf_sym]["Close"]

            close_series = close_series.dropna()
            if close_series.empty:
                log.warning("No data returned for %s", original)
                results[original] = (None, None)
                continue

            last_close = float(close_series.iloc[-1])
            last_date  = close_series.index[-1].strftime("%Y-%m-%d")
            log.info("%-8s  %s  %.0f", original, last_date, last_close)
            results[original] = (last_close, last_date)

        except Exception as exc:
            log.warning("Could not parse data for %s: %s", original, exc)
            results[original] = (None, None)

    return results


# ── Sheet read / write ─────────────────────────────────────────────────────
def get_tickers_from_sheet(ws: gspread.Worksheet, cfg: dict) -> list[tuple[int, str]]:
    """
    Read the ticker column and return a list of (row_index_0based, ticker).
    Rows above header_rows are skipped; empty cells are ignored.
    """
    col_values = ws.col_values(cfg["ticker_col"] + 1)  # gspread is 1-indexed
    pairs = []
    for row_idx, cell in enumerate(col_values):
        if row_idx < cfg["header_rows"]:
            continue
        ticker = str(cell).strip()
        if ticker:
            pairs.append((row_idx, ticker))
    return pairs


def write_prices_to_sheet(
    ws: gspread.Worksheet,
    cfg: dict,
    ticker_rows: list[tuple[int, str]],
    prices: dict[str, tuple[float | None, str | None]],
) -> None:
    """
    Build a batch update and send it in a single API call.
    Only rows where we have a price are updated; failed tickers are skipped.
    """
    timestamp = datetime.now(ICT).strftime("%Y-%m-%d %H:%M ICT")
    updates = []

    for row_idx, ticker in ticker_rows:
        price, price_date = prices.get(ticker, (None, None))
        if price is None:
            log.warning("Skipping write for %s — no price available.", ticker)
            continue

        # gspread batch_update expects A1 notation; rows/cols are 1-indexed
        sheet_row = row_idx + 1
        price_cell = gspread.utils.rowcol_to_a1(sheet_row, cfg["price_col"] + 1)
        date_cell  = gspread.utils.rowcol_to_a1(sheet_row, cfg["date_col"] + 1)

        updates.append({"range": price_cell, "values": [[price]]})
        updates.append({"range": date_cell,  "values": [[price_date]]})

    if not updates:
        log.warning("Nothing to write — no prices were fetched successfully.")
        return

    ws.batch_update(updates, value_input_option="USER_ENTERED")
    log.info("Wrote %d price+date pairs to sheet. (run at %s)", len(updates) // 2, timestamp)


# ── Main ───────────────────────────────────────────────────────────────────
def main() -> None:
    log.info("=== VN Portfolio Price Updater starting ===")

    # 1. Connect to Google Sheets
    log.info("Authenticating to Google Sheets...")
    gc = get_gspread_client()
    sh = gc.open_by_key(SHEET_CONFIG["spreadsheet_id"])
    ws = sh.worksheet(SHEET_CONFIG["worksheet_name"])
    log.info("Opened sheet: '%s' → tab: '%s'", sh.title, ws.title)

    # 2. Read tickers from the sheet
    ticker_rows = get_tickers_from_sheet(ws, SHEET_CONFIG)
    if not ticker_rows:
        log.warning("No tickers found in column %s. Exiting.", SHEET_CONFIG["ticker_col"])
        return
    tickers = [t for _, t in ticker_rows]
    log.info("Found %d tickers: %s", len(tickers), tickers)

    # 3. Fetch closing prices from Yahoo Finance
    log.info("Fetching prices from Yahoo Finance...")
    prices = fetch_closing_prices(tickers)

    # 4. Write back to sheet
    log.info("Writing prices to sheet...")
    write_prices_to_sheet(ws, SHEET_CONFIG, ticker_rows, prices)

    log.info("=== Done ===")


if __name__ == "__main__":
    main()

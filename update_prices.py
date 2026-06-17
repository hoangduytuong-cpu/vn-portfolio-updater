"""
update_prices.py
────────────────
Fetches the latest closing prices for Vietnamese stocks and writes them
into a Google Sheet.

Data sources (chosen per ticker based on an "Exchange" column in the sheet):
  • yfinance (Yahoo Finance)  — for HOSE and HNX tickers
  • vnstock  (TCBS backend)   — for UPCoM tickers (e.g. F88)

Sheet layout (columns are 0-indexed; edit SHEET_CONFIG to match yours):
  Col A (0): Ticker   e.g. VIC, LPB, F88
  Col B (1): Exchange e.g. HOSE, HNX, or UPCOM   ← NEW column
  Col C (2): Last Close price   (written by this script)
  Col D (3): Price Date         (written by this script)
  Row 1    : headers — skipped automatically
"""

import os
import json
import logging
from datetime import datetime, timedelta, timezone, date

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

# ── Sheet layout — edit to match YOUR sheet ────────────────────────────────
SHEET_CONFIG = {
    "spreadsheet_id":  os.environ["SPREADSHEET_ID"],
    "worksheet_name":  "Portfolio",
    "header_rows":     1,
    "ticker_col":      0,   # col A
    "exchange_col":    1,   # col B  ← NEW: HOSE / HNX / UPCOM
    "price_col":       2,   # col C
    "date_col":        3,   # col D
}

# Vietnam timezone (ICT = UTC+7)
ICT = timezone(timedelta(hours=7))

# Google Sheets API scopes
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]


# ── Auth ───────────────────────────────────────────────────────────────────
def get_gspread_client() -> gspread.Client:
    creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    return gspread.authorize(creds)


# ── yfinance: HOSE & HNX ───────────────────────────────────────────────────
def fetch_via_yfinance(tickers: list[str]) -> dict[str, tuple[float | None, str | None]]:
    """Fetch closing prices via Yahoo Finance using the .VN suffix."""
    if not tickers:
        return {}

    yf_map = {f"{t.upper()}.VN": t for t in tickers}   # "VIC.VN" → "VIC"
    results: dict[str, tuple[float | None, str | None]] = {t: (None, None) for t in tickers}

    try:
        yf_symbols = list(yf_map.keys())
        raw = yf.download(
            tickers=yf_symbols,
            period="5d",
            interval="1d",
            auto_adjust=True,
            progress=False,
            group_by="ticker",
        )
    except Exception as exc:
        log.error("yfinance batch download failed: %s", exc)
        return results

    for yf_sym, original in yf_map.items():
        try:
            if len(yf_symbols) == 1:
                close_series = raw["Close"]
            else:
                close_series = raw[yf_sym]["Close"]

            close_series = close_series.dropna()
            if close_series.empty:
                log.warning("yfinance: no data for %s", original)
                continue

            price = float(close_series.iloc[-1])
            dt    = close_series.index[-1].strftime("%Y-%m-%d")
            log.info("yfinance  %-8s  %s  %.0f", original, dt, price)
            results[original] = (price, dt)

        except Exception as exc:
            log.warning("yfinance: parse error for %s — %s", original, exc)

    return results


# ── vnstock: UPCoM ─────────────────────────────────────────────────────────
def fetch_via_vnstock(tickers: list[str]) -> dict[str, tuple[float | None, str | None]]:
    """
    Fetch closing prices for UPCoM stocks via vnstock (TCBS backend).
    vnstock is a Vietnam-native library; TCBS carries UPCoM stocks reliably.
    """
    if not tickers:
        return {}

    results: dict[str, tuple[float | None, str | None]] = {t: (None, None) for t in tickers}

    try:
        from vnstock import Vnstock
    except ImportError:
        log.error("vnstock not installed — add it to requirements.txt")
        return results

    # Determine date range: last 5 calendar days to catch any holidays
    end_dt   = date.today()
    start_dt = end_dt - timedelta(days=7)
    end_str   = end_dt.strftime("%Y-%m-%d")
    start_str = start_dt.strftime("%Y-%m-%d")

    for ticker in tickers:
        try:
            stock = Vnstock().stock(symbol=ticker.upper(), source="TCBS")
            df = stock.quote.history(
                start=start_str,
                end=end_str,
                interval="1D",
            )
            if df is None or df.empty:
                log.warning("vnstock: no data for %s", ticker)
                continue

            # vnstock returns columns: time, open, high, low, close, volume
            close_col = next(
                (c for c in df.columns if c.lower() in ("close", "close_price")), None
            )
            time_col = next(
                (c for c in df.columns if c.lower() in ("time", "date", "trading_date")), None
            )
            if close_col is None:
                log.warning("vnstock: could not identify close column for %s. Cols: %s", ticker, list(df.columns))
                continue

            df = df.dropna(subset=[close_col])
            if df.empty:
                log.warning("vnstock: all-NaN close for %s", ticker)
                continue

            price = float(df[close_col].iloc[-1])
            # Price from TCBS is in thousands VND — multiply by 1000
            price = price * 1000

            if time_col:
                raw_date = df[time_col].iloc[-1]
                # Handle both datetime and string date values
                if hasattr(raw_date, "strftime"):
                    dt = raw_date.strftime("%Y-%m-%d")
                else:
                    dt = str(raw_date)[:10]
            else:
                dt = end_str

            log.info("vnstock   %-8s  %s  %.0f", ticker, dt, price)
            results[ticker] = (price, dt)

        except Exception as exc:
            log.warning("vnstock: error for %s — %s", ticker, exc)

    return results


# ── Sheet read ─────────────────────────────────────────────────────────────
def get_ticker_rows(ws: gspread.Worksheet, cfg: dict) -> list[tuple[int, str, str]]:
    """
    Returns list of (row_idx_0based, ticker, exchange).
    Exchange is normalised to uppercase: HOSE, HNX, or UPCOM.
    If exchange column is blank, defaults to HOSE.
    """
    ticker_col   = ws.col_values(cfg["ticker_col"]   + 1)
    exchange_col = ws.col_values(cfg["exchange_col"] + 1)

    rows = []
    for i, (ticker, exchange) in enumerate(
        zip(ticker_col, exchange_col + [""] * len(ticker_col))
    ):
        if i < cfg["header_rows"]:
            continue
        ticker = str(ticker).strip().upper()
        if not ticker:
            continue
        exchange = str(exchange).strip().upper() or "HOSE"
        rows.append((i, ticker, exchange))
    return rows


# ── Sheet write ────────────────────────────────────────────────────────────
def write_prices(
    ws: gspread.Worksheet,
    cfg: dict,
    ticker_rows: list[tuple[int, str, str]],
    prices: dict[str, tuple[float | None, str | None]],
) -> None:
    updates = []
    for row_idx, ticker, _ in ticker_rows:
        price, dt = prices.get(ticker, (None, None))
        if price is None:
            log.warning("No price — skipping write for %s", ticker)
            continue
        sheet_row  = row_idx + 1
        price_cell = gspread.utils.rowcol_to_a1(sheet_row, cfg["price_col"] + 1)
        date_cell  = gspread.utils.rowcol_to_a1(sheet_row, cfg["date_col"]  + 1)
        updates.append({"range": price_cell, "values": [[price]]})
        updates.append({"range": date_cell,  "values": [[dt]]})

    if not updates:
        log.warning("Nothing to write.")
        return

    ws.batch_update(updates, value_input_option="USER_ENTERED")
    run_time = datetime.now(ICT).strftime("%Y-%m-%d %H:%M ICT")
    log.info("Wrote %d price+date pairs to sheet at %s", len(updates) // 2, run_time)


# ── Main ───────────────────────────────────────────────────────────────────
def main() -> None:
    log.info("=== VN Portfolio Price Updater starting ===")

    # 1. Connect
    gc = get_gspread_client()
    sh = gc.open_by_key(SHEET_CONFIG["spreadsheet_id"])
    ws = sh.worksheet(SHEET_CONFIG["worksheet_name"])
    log.info("Sheet: '%s' → tab: '%s'", sh.title, ws.title)

    # 2. Read tickers + exchanges
    ticker_rows = get_ticker_rows(ws, SHEET_CONFIG)
    if not ticker_rows:
        log.warning("No tickers found. Exiting.")
        return
    log.info("Found %d tickers: %s", len(ticker_rows), [(t, ex) for _, t, ex in ticker_rows])

    # 3. Route by exchange
    yf_tickers     = [t for _, t, ex in ticker_rows if ex in ("HOSE", "HNX")]
    upcom_tickers  = [t for _, t, ex in ticker_rows if ex == "UPCOM"]

    prices: dict[str, tuple[float | None, str | None]] = {}

    if yf_tickers:
        log.info("Fetching %d HOSE/HNX tickers via yfinance...", len(yf_tickers))
        prices.update(fetch_via_yfinance(yf_tickers))

    if upcom_tickers:
        log.info("Fetching %d UPCoM tickers via vnstock (TCBS)...", len(upcom_tickers))
        prices.update(fetch_via_vnstock(upcom_tickers))

    # 4. Write
    write_prices(ws, SHEET_CONFIG, ticker_rows, prices)
    log.info("=== Done ===")


if __name__ == "__main__":
    main()

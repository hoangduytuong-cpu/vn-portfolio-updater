"""
update_prices.py
────────────────
Fetches the latest stock prices for Vietnamese stocks and writes them
into a Google Sheet.

Data sources routed by exchange:
  HOSE / HNX  →  yfinance  (Yahoo Finance, .VN suffix)
  UPCOM       →  vnstock   (KBS backend — KB Securities, no API key needed)

Sheet layout (0-indexed columns — edit SHEET_CONFIG to match yours):
  Col A (0): Ticker     e.g.  VIC, LPB, F88
  Col B (1): Exchange   e.g.  HOSE, HNX, UPCOM
  Col C (2): Price      ← written by this script
  Col D (3): Date       ← written by this script
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

# ── Sheet layout — edit these to match YOUR sheet ──────────────────────────
SHEET_CONFIG = {
    "spreadsheet_id":  os.environ["SPREADSHEET_ID"],
    "worksheet_name":  "Portfolio",   # tab name inside the sheet
    "header_rows":     1,             # rows to skip at the top
    "ticker_col":      0,             # col A
    "exchange_col":    1,             # col B  (HOSE / HNX / UPCOM)
    "price_col":       2,             # col C
    "date_col":        3,             # col D
}

# Vietnam timezone (ICT = UTC+7)
ICT = timezone(timedelta(hours=7))

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]


# ── Google Sheets auth ─────────────────────────────────────────────────────
def get_gspread_client() -> gspread.Client:
    creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    return gspread.authorize(creds)


# ── yfinance: HOSE & HNX ───────────────────────────────────────────────────
def fetch_via_yfinance(tickers: list[str]) -> dict[str, tuple[float | None, str | None]]:
    """
    Fetch the latest available price for HOSE/HNX tickers via Yahoo Finance.
    Uses period='1d' with interval='1m' so we get the true latest tick —
    this works both during market hours (live) and after close (last close).
    Falls back to daily OHLC if the 1m endpoint returns nothing.
    """
    if not tickers:
        return {}

    yf_map   = {f"{t.upper()}.VN": t for t in tickers}
    results  = {t: (None, None) for t in tickers}
    yf_syms  = list(yf_map.keys())

    def _parse_series(series, original):
        series = series.dropna()
        if series.empty:
            return None, None
        price = float(series.iloc[-1])
        # use today's date for intraday, otherwise the bar date
        try:
            dt = series.index[-1].strftime("%Y-%m-%d")
        except Exception:
            dt = date.today().strftime("%Y-%m-%d")
        log.info("yfinance  %-8s  %s  %.0f", original, dt, price)
        return price, dt

    # Primary: 1-minute bars for today (captures live + post-close)
    try:
        raw_1m = yf.download(
            tickers=yf_syms,
            period="1d",
            interval="1m",
            auto_adjust=True,
            progress=False,
            group_by="ticker",
        )
        for yf_sym, original in yf_map.items():
            try:
                close = raw_1m[yf_sym]["Close"] if len(yf_syms) > 1 else raw_1m["Close"]
                price, dt = _parse_series(close, original)
                if price is not None:
                    results[original] = (price, dt)
            except Exception:
                pass  # fall through to daily fallback below
    except Exception as exc:
        log.warning("yfinance 1m download error: %s — falling back to daily", exc)

    # Fallback: daily bars for tickers that 1m missed
    missing = [t for t in tickers if results[t][0] is None]
    if missing:
        missing_yf = [f"{t.upper()}.VN" for t in missing]
        try:
            raw_1d = yf.download(
                tickers=missing_yf,
                period="5d",
                interval="1d",
                auto_adjust=True,
                progress=False,
                group_by="ticker",
            )
            for yf_sym in missing_yf:
                original = yf_map[yf_sym]
                try:
                    close = raw_1d[yf_sym]["Close"] if len(missing_yf) > 1 else raw_1d["Close"]
                    price, dt = _parse_series(close, original)
                    if price is not None:
                        results[original] = (price, dt)
                except Exception as exc:
                    log.warning("yfinance daily fallback failed for %s: %s", original, exc)
        except Exception as exc:
            log.error("yfinance daily fallback download error: %s", exc)

    return results


# ── vnstock: UPCoM (F88 etc.) ──────────────────────────────────────────────
def fetch_via_vnstock(tickers: list[str]) -> dict[str, tuple[float | None, str | None]]:
    """
    Fetch prices for UPCoM stocks via vnstock v4 using the KBS (KB Securities)
    data source. KBS requires no API key and covers all HOSE/HNX/UPCoM stocks
    including F88.

    vnstock v4 dropped TCBS as a supported source (August 2025). The new
    API is vnstock.api.quote.Quote with source='KBS'.

    KBS prices are in full VND (not thousands), so no multiplication needed.
    """
    if not tickers:
        return {}

    results = {t: (None, None) for t in tickers}

    try:
        from vnstock.api.quote import Quote
    except ImportError:
        log.error("vnstock not installed or too old — ensure vnstock>=4.0.0 in requirements.txt")
        return results

    today_str = date.today().strftime("%Y-%m-%d")
    # 10-day window to safely cover weekends + public holidays
    start_str = (date.today() - timedelta(days=10)).strftime("%Y-%m-%d")

    for ticker in tickers:
        try:
            q = Quote(symbol=ticker.upper(), source="KBS")

            df = q.history(start=start_str, end=today_str, interval="1D")

            if df is None or df.empty:
                log.warning("vnstock KBS: no data for %s", ticker)
                continue

            # KBS columns: time, open, high, low, close, volume
            close_col = next(
                (c for c in df.columns if c.lower() in ("close", "close_price")), None
            )
            time_col = next(
                (c for c in df.columns if c.lower() in ("time", "date", "trading_date")), None
            )

            if close_col is None:
                log.warning("vnstock KBS: unrecognised columns for %s: %s", ticker, list(df.columns))
                continue

            df = df.dropna(subset=[close_col])
            if df.empty:
                log.warning("vnstock KBS: all-NaN close for %s", ticker)
                continue

            price = float(df[close_col].iloc[-1])  # KBS prices are full VND

            if time_col:
                raw_date = df[time_col].iloc[-1]
                dt = raw_date.strftime("%Y-%m-%d") if hasattr(raw_date, "strftime") else str(raw_date)[:10]
            else:
                dt = today_str

            log.info("vnstock KBS  %-8s  %s  %.0f", ticker, dt, price)
            results[ticker] = (price, dt)

        except Exception as exc:
            log.warning("vnstock KBS: error for %s — %s", ticker, exc)

    return results


# ── Sheet read ─────────────────────────────────────────────────────────────
def get_ticker_rows(ws: gspread.Worksheet, cfg: dict) -> list[tuple[int, str, str]]:
    """
    Returns [(row_idx_0based, ticker, exchange), ...].
    Exchange normalised to uppercase; blank defaults to HOSE.
    """
    ticker_vals   = ws.col_values(cfg["ticker_col"]   + 1)
    exchange_vals = ws.col_values(cfg["exchange_col"] + 1)

    # Pad exchange list in case it's shorter than ticker list
    exchange_vals += [""] * max(0, len(ticker_vals) - len(exchange_vals))

    rows = []
    for i, (ticker, exchange) in enumerate(zip(ticker_vals, exchange_vals)):
        if i < cfg["header_rows"]:
            continue
        ticker   = str(ticker).strip().upper()
        exchange = str(exchange).strip().upper() or "HOSE"
        if ticker:
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
            log.warning("No price — skipping %s", ticker)
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
    ts = datetime.now(ICT).strftime("%Y-%m-%d %H:%M ICT")
    log.info("Wrote %d tickers to sheet at %s", len(updates) // 2, ts)


# ── Main ───────────────────────────────────────────────────────────────────
def main() -> None:
    log.info("=== VN Portfolio Price Updater starting ===")

    gc = get_gspread_client()
    sh = gc.open_by_key(SHEET_CONFIG["spreadsheet_id"])
    ws = sh.worksheet(SHEET_CONFIG["worksheet_name"])
    log.info("Sheet: '%s' → tab: '%s'", sh.title, ws.title)

    ticker_rows = get_ticker_rows(ws, SHEET_CONFIG)
    if not ticker_rows:
        log.warning("No tickers found. Exiting.")
        return
    log.info("Tickers: %s", [(t, ex) for _, t, ex in ticker_rows])

    yf_tickers    = [t for _, t, ex in ticker_rows if ex in ("HOSE", "HNX")]
    upcom_tickers = [t for _, t, ex in ticker_rows if ex == "UPCOM"]

    prices: dict[str, tuple[float | None, str | None]] = {}

    if yf_tickers:
        log.info("Fetching %d HOSE/HNX tickers via yfinance...", len(yf_tickers))
        prices.update(fetch_via_yfinance(yf_tickers))

    if upcom_tickers:
        log.info("Fetching %d UPCoM tickers via vnstock (KBS)...", len(upcom_tickers))
        prices.update(fetch_via_vnstock(upcom_tickers))

    write_prices(ws, SHEET_CONFIG, ticker_rows, prices)
    log.info("=== Done ===")


if __name__ == "__main__":
    main()

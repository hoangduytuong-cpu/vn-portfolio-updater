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
  Col B (1): Exchange   e.g.  HOSE, HNX, UPCOM  ← auto-filled if blank
  Col C (2): Price      ← written by this script
  Col D (3): Date       ← written by this script
  Row 1    : headers — skipped automatically

Column B is optional — if blank for a ticker, the script looks up the
exchange automatically from KBS and writes it back to the sheet.
Just add a ticker to column A and leave everything else empty.
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
    "worksheet_name":  "Updater",   # tab name inside the sheet
    "header_rows":     1,             # rows to skip at the top
    "ticker_col":      0,             # col A
    "exchange_col":    1,             # col B  (auto-filled if blank)
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


# ── Exchange auto-detection ────────────────────────────────────────────────
def build_exchange_map() -> dict[str, str]:
    """
    Fetch the full market listing from KBS and return a dict:
        { 'VIC': 'HOSE', 'SHB': 'HNX', 'F88': 'UPCOM', ... }

    Uses Listing(source='KBS').symbols_by_exchange() which returns a DataFrame
    with 'symbol' and 'exchange' columns covering all three exchanges.
    Falls back to an empty dict if the API call fails — callers handle that.
    """
    try:
        from vnstock.api.listing import Listing
        df = Listing(source="KBS").symbols_by_exchange()
        if df is None or df.empty:
            log.warning("Exchange map: empty response from KBS listing API")
            return {}
        # Normalise to uppercase
        exchange_map = {
            str(row["symbol"]).upper(): str(row["exchange"]).upper()
            for _, row in df.iterrows()
            if row.get("symbol") and row.get("exchange")
        }
        log.info("Exchange map loaded: %d tickers", len(exchange_map))
        return exchange_map
    except Exception as exc:
        log.warning("Could not build exchange map: %s", exc)
        return {}


def fill_missing_exchanges(
    ws: gspread.Worksheet,
    cfg: dict,
    ticker_rows: list[tuple[int, str, str]],
    exchange_map: dict[str, str],
) -> list[tuple[int, str, str]]:
    """
    For any ticker whose exchange cell is blank, look it up in exchange_map
    and write the result back to the sheet in a single batch call.

    Returns an updated ticker_rows list with resolved exchanges.
    """
    updates = []
    resolved = []

    for row_idx, ticker, exchange in ticker_rows:
        if exchange:
            resolved.append((row_idx, ticker, exchange))
            continue

        # Exchange is blank — try to resolve
        looked_up = exchange_map.get(ticker)
        if looked_up:
            sheet_row    = row_idx + 1
            exchange_cell = gspread.utils.rowcol_to_a1(sheet_row, cfg["exchange_col"] + 1)
            updates.append({"range": exchange_cell, "values": [[looked_up]]})
            log.info("Auto-detected exchange for %s: %s", ticker, looked_up)
            resolved.append((row_idx, ticker, looked_up))
        else:
            log.warning(
                "Cannot detect exchange for '%s' — not found in KBS listing. "
                "Please fill column B manually for this ticker.", ticker
            )
            resolved.append((row_idx, ticker, ""))  # leave blank, will be skipped in routing

    if updates:
        ws.batch_update(updates, value_input_option="USER_ENTERED")
        log.info("Wrote %d auto-detected exchange(s) to sheet.", len(updates))

    return resolved


# ── yfinance: HOSE & HNX ───────────────────────────────────────────────────
def fetch_via_yfinance(tickers: list[str]) -> dict[str, tuple[float | None, str | None]]:
    """
    Fetch the latest available price for HOSE/HNX tickers via Yahoo Finance.
    Uses 1-minute bars for today to capture live price; falls back to daily.
    """
    if not tickers:
        return {}

    yf_map  = {f"{t.upper()}.VN": t for t in tickers}
    results = {t: (None, None) for t in tickers}
    yf_syms = list(yf_map.keys())

    def _parse_series(series, original):
        series = series.dropna()
        if series.empty:
            return None, None
        price = float(series.iloc[-1])
        try:
            dt = series.index[-1].strftime("%Y-%m-%d")
        except Exception:
            dt = date.today().strftime("%Y-%m-%d")
        log.info("yfinance  %-8s  %s  %.0f", original, dt, price)
        return price, dt

    # Primary: 1-minute bars for today
    try:
        raw_1m = yf.download(
            tickers=yf_syms, period="1d", interval="1m",
            auto_adjust=True, progress=False, group_by="ticker",
        )
        for yf_sym, original in yf_map.items():
            try:
                close = raw_1m[yf_sym]["Close"] if len(yf_syms) > 1 else raw_1m["Close"]
                price, dt = _parse_series(close, original)
                if price is not None:
                    results[original] = (price, dt)
            except Exception:
                pass
    except Exception as exc:
        log.warning("yfinance 1m error: %s — falling back to daily", exc)

    # Fallback: daily bars for anything still missing
    missing = [t for t in tickers if results[t][0] is None]
    if missing:
        missing_yf = [f"{t.upper()}.VN" for t in missing]
        try:
            raw_1d = yf.download(
                tickers=missing_yf, period="5d", interval="1d",
                auto_adjust=True, progress=False, group_by="ticker",
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
            log.error("yfinance daily fallback error: %s", exc)

    return results


# ── vnstock: UPCoM (F88 etc.) ──────────────────────────────────────────────
def fetch_via_vnstock(tickers: list[str]) -> dict[str, tuple[float | None, str | None]]:
    """
    Fetch prices for UPCoM stocks via vnstock v4 using the KBS data source.
    KBS requires no API key and covers all HOSE/HNX/UPCoM stocks including F88.
    Prices from KBS are in full VND.
    """
    if not tickers:
        return {}

    results = {t: (None, None) for t in tickers}

    try:
        from vnstock.api.quote import Quote
    except ImportError:
        log.error("vnstock not installed — ensure vnstock>=4.0.0 in requirements.txt")
        return results

    today_str = date.today().strftime("%Y-%m-%d")
    start_str = (date.today() - timedelta(days=10)).strftime("%Y-%m-%d")

    for ticker in tickers:
        try:
            q  = Quote(symbol=ticker.upper(), source="KBS")
            df = q.history(start=start_str, end=today_str, interval="1D")

            if df is None or df.empty:
                log.warning("vnstock KBS: no data for %s", ticker)
                continue

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
                continue

            price = float(df[close_col].iloc[-1]) * 1000

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
    Exchange is normalised to uppercase; blank is kept as "" for auto-detection.
    """
    ticker_vals   = ws.col_values(cfg["ticker_col"]   + 1)
    exchange_vals = ws.col_values(cfg["exchange_col"] + 1)
    exchange_vals += [""] * max(0, len(ticker_vals) - len(exchange_vals))

    rows = []
    for i, (ticker, exchange) in enumerate(zip(ticker_vals, exchange_vals)):
        if i < cfg["header_rows"]:
            continue
        ticker   = str(ticker).strip().upper()
        exchange = str(exchange).strip().upper()
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
        time_cell = gspread.utils.rowcol_to_a1(sheet_row, cfg["date_col"] + 2)
        updates.append({"range": time_cell, "values": [[datetime.now(ICT).strftime("%H:%M")]]})

    if not updates:
        log.warning("Nothing to write.")
        return

    ws.batch_update(updates, value_input_option="USER_ENTERED")
    ts = datetime.now(ICT).strftime("%Y-%m-%d %H:%M ICT")
    log.info("Wrote %d tickers to sheet at %s", len(updates) // 2, ts)


# ── Main ───────────────────────────────────────────────────────────────────
def main() -> None:
    log.info("=== VN Portfolio Price Updater starting ===")

    # 1. Connect to Google Sheets
    gc = get_gspread_client()
    sh = gc.open_by_key(SHEET_CONFIG["spreadsheet_id"])
    ws = sh.worksheet(SHEET_CONFIG["worksheet_name"])
    log.info("Sheet: '%s' → tab: '%s'", sh.title, ws.title)

    # 2. Read tickers (exchange may be blank for new entries)
    ticker_rows = get_ticker_rows(ws, SHEET_CONFIG)
    if not ticker_rows:
        log.warning("No tickers found. Exiting.")
        return

    # 3. Auto-fill any blank exchange cells
    blanks = [t for _, t, ex in ticker_rows if not ex]
    if blanks:
        log.info("Blank exchange for: %s — fetching market listing...", blanks)
        exchange_map = build_exchange_map()
        ticker_rows  = fill_missing_exchanges(ws, SHEET_CONFIG, ticker_rows, exchange_map)
    
    log.info("Tickers: %s", [(t, ex) for _, t, ex in ticker_rows])

    # 4. Fetch all tickers via KBS regardless of exchange
    all_tickers = [t for _, t, ex in ticker_rows if ex in ("HOSE", "HNX", "UPCOM")]
    unknown     = [t for _, t, ex in ticker_rows if ex not in ("HOSE", "HNX", "UPCOM")]
    if unknown:
        log.warning("Skipping tickers with unresolved exchange: %s", unknown)

    prices: dict[str, tuple[float | None, str | None]] = {}

    if all_tickers:
        log.info("Fetching %d tickers via vnstock (KBS)...", len(all_tickers))
        prices.update(fetch_via_vnstock(all_tickers))

    still_missing = [t for t in (yf_tickers) if prices.get(t, (None, None))[0] is None]
    if still_missing:
        log.info("yfinance failed for %s — retrying via KBS...", still_missing)
        prices.update(fetch_via_vnstock(still_missing))

    # 5. Write prices
    write_prices(ws, SHEET_CONFIG, ticker_rows, prices)
    log.info("=== Done ===")


if __name__ == "__main__":
    main()

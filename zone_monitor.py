"""Two-stage zone monitor for paper-trading review.

This tool is intentionally alert-only. It does not place broker or TradingView
orders. It watches 15-minute candles, uses a 4-hour EMA trend filter, and
emits two events per zone:

* ZONE_TOUCH: price touches a Soli line or enters a configured rectangle.
* CONFIRMED_15M: after a touch, a 15m rejection plus short-term structure
  break agrees with the 4h trend.

The private SVA indicator is not reconstructed here. The confirmation message
therefore explicitly asks for a manual SVA check unless a future provider is
added.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import requests


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def load_local_env(path: Path) -> None:
    """Load simple KEY=VALUE pairs without requiring python-dotenv."""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


def save_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def fetch_15m(ticker: str, lookback_days: int = 5) -> pd.DataFrame:
    try:
        import yfinance as yf
    except ImportError as exc:
        raise RuntimeError("yfinance is not installed; run: pip install -r requirements.txt") from exc

    period = f"{max(2, min(59, lookback_days))}d"
    frame = yf.download(
        tickers=ticker,
        period=period,
        interval="15m",
        auto_adjust=False,
        progress=False,
        threads=False,
    )
    if frame is None or frame.empty:
        raise RuntimeError(f"no 15m data returned for {ticker}")

    if isinstance(frame.columns, pd.MultiIndex):
        # yfinance may return a two-level column index even for one ticker.
        frame = frame.xs(ticker, axis=1, level=-1, drop_level=True)
    frame.columns = [str(column).strip().lower() for column in frame.columns]
    required = ["open", "high", "low", "close"]
    missing = [column for column in required if column not in frame.columns]
    if missing:
        raise RuntimeError(f"missing OHLC columns for {ticker}: {missing}")

    frame = frame[required + (["volume"] if "volume" in frame.columns else [])].dropna()
    frame.index = pd.to_datetime(frame.index)
    if getattr(frame.index, "tz", None) is not None:
        frame.index = frame.index.tz_convert("UTC").tz_localize(None)
    frame = frame.sort_index()
    # Keep the newest candle for early zone-touch detection. The caller uses
    # only the preceding closed candles for the 15m confirmation.
    return frame


def resample_4h(frame: pd.DataFrame) -> pd.DataFrame:
    aggregation: dict[str, str] = {
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
    }
    if "volume" in frame.columns:
        aggregation["volume"] = "sum"
    return frame.resample("4h", origin="start_day").agg(aggregation).dropna()


def trend_bias(frame: pd.DataFrame) -> str:
    candles = resample_4h(frame)
    if len(candles) < 55:
        return "unknown"
    close = candles["close"]
    ema20 = close.ewm(span=20, adjust=False).mean().iloc[-1]
    ema50 = close.ewm(span=50, adjust=False).mean().iloc[-1]
    last = close.iloc[-1]
    if ema20 > ema50 and last > ema20:
        return "bullish"
    if ema20 < ema50 and last < ema20:
        return "bearish"
    return "neutral"


def zone_hit(candle: pd.Series, zone: dict[str, Any]) -> bool:
    low = float(candle["low"])
    high = float(candle["high"])
    line = zone.get("line")
    if line is not None and low <= float(line) <= high:
        return True
    lower = zone.get("lower")
    upper = zone.get("upper")
    if lower is None or upper is None:
        return False
    return low <= float(upper) and high >= float(lower)


def confirmation(frame: pd.DataFrame, zone: dict[str, Any], bias: str, structure_bars: int) -> bool:
    if len(frame) < max(8, structure_bars + 2):
        return False
    current = frame.iloc[-1]
    previous = frame.iloc[-(structure_bars + 1) : -1]
    side = str(zone["side"]).lower()
    line = zone.get("line")
    lower = zone.get("lower")
    upper = zone.get("upper")
    reference = float(line) if line is not None else None

    if lower is not None and upper is not None:
        lower_value = float(lower)
        upper_value = float(upper)
    elif reference is not None:
        lower_value = upper_value = reference
    else:
        return False

    bullish_candle = float(current["close"]) > float(current["open"])
    bearish_candle = float(current["close"]) < float(current["open"])
    if side == "buy":
        reaction = bullish_candle and float(current["close"]) > upper_value
        structure_break = float(current["close"]) > float(previous["high"].max())
        return bias == "bullish" and reaction and structure_break
    if side == "sell":
        reaction = bearish_candle and float(current["close"]) < lower_value
        structure_break = float(current["close"]) < float(previous["low"].min())
        return bias == "bearish" and reaction and structure_break
    return False


def format_event(symbol: str, zone: dict[str, Any], event: str, price: float, bias: str) -> str:
    side = str(zone["side"]).upper()
    label = zone.get("label", zone.get("id", "zone"))
    sva_note = "SVA را روی TradingView دستی تأیید کن"
    if event == "ZONE_TOUCH":
        return (
            f"⚠️ ZONE TOUCH — {symbol}\n"
            f"جهت: {side} | ناحیه: {label}\n"
            f"قیمت: {price:.8g} | روند 4H: {bias}\n"
            "این فقط هشدار بررسی است؛ هنوز سیگنال ورود نیست."
        )
    return (
        f"✅ CONFIRMED 15M — {symbol}\n"
        f"جهت: {side} | ناحیه: {label}\n"
        f"قیمت: {price:.8g} | روند 4H: {bias}\n"
        f"تأیید Price Action و شکست ساختار دیده شد؛ {sva_note}.\n"
        "قبل از هر Paper Trade، حدضرر و نسبت ریسک‌به‌پاداش را بررسی کن."
    )


def send_telegram(message: str) -> str:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return "telegram not configured; printed locally"
    response = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": message},
        timeout=15,
    )
    response.raise_for_status()
    return "telegram sent"


def append_log(path: Path, event: str, symbol: str, zone_id: str, price: float, bias: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "timestamp_utc": utc_now(),
        "event": event,
        "symbol": symbol,
        "zone_id": zone_id,
        "price": price,
        "trend_4h": bias,
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def run_cycle(config_path: Path, state_path: Path, log_path: Path) -> int:
    config = load_json(config_path, {})
    state = load_json(state_path, {"zones": {}})
    state.setdefault("zones", {})
    emitted = 0

    for instrument in config.get("symbols", []):
        if not instrument.get("enabled", True):
            continue
        symbol = str(instrument["id"])
        ticker = str(instrument["data_symbol"])
        try:
            frame = fetch_15m(ticker, int(config.get("lookback_days", 5)))
            if frame.empty:
                continue
            if len(frame) > 1:
                closed_frame = frame.iloc[:-1]
            else:
                closed_frame = frame
            bias = trend_bias(closed_frame)
            # The newest Yahoo candle may still be forming. This is intentional:
            # it reduces zone-touch alert latency. Confirmation remains closed-bar only.
            candle = frame.iloc[-1]
        except Exception as exc:  # one bad symbol must not stop the watchlist
            print(f"[ERROR] {symbol}: {exc}", file=sys.stderr)
            continue

        for zone in instrument.get("zones", []):
            if not zone.get("enabled", True):
                continue
            zone_id = str(zone["id"])
            key = f"{symbol}:{zone_id}"
            lifecycle = state["zones"].setdefault(
                key, {"touch_sent": False, "confirm_sent": False, "outside_bars": 0}
            )
            hit = zone_hit(candle, zone)
            price = float(candle["close"])
            if hit:
                lifecycle["outside_bars"] = 0
                if not lifecycle["touch_sent"]:
                    message = format_event(symbol, zone, "ZONE_TOUCH", price, bias)
                    print(message)
                    print(send_telegram(message))
                    append_log(log_path, "ZONE_TOUCH", symbol, zone_id, price, bias)
                    lifecycle["touch_sent"] = True
                    emitted += 1
                if lifecycle["touch_sent"] and not lifecycle["confirm_sent"]:
                    if confirmation(closed_frame, zone, bias, int(config["confirm"].get("structure_bars", 3))):
                        message = format_event(symbol, zone, "CONFIRMED_15M", price, bias)
                        print(message)
                        print(send_telegram(message))
                        append_log(log_path, "CONFIRMED_15M", symbol, zone_id, price, bias)
                        lifecycle["confirm_sent"] = True
                        emitted += 1
            else:
                lifecycle["outside_bars"] = int(lifecycle.get("outside_bars", 0)) + 1
                if lifecycle["outside_bars"] >= int(config.get("rearm_after_bars", 3)):
                    lifecycle["touch_sent"] = False
                    lifecycle["confirm_sent"] = False

    state["last_cycle_utc"] = utc_now()
    save_json(state_path, state)
    return emitted


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(Path(__file__).with_name("zones.json")))
    parser.add_argument("--state", default=str(Path(__file__).with_name("state.json")))
    parser.add_argument("--log", default=str(Path(__file__).with_name("alerts.jsonl")))
    parser.add_argument("--once", action="store_true", help="run one scan and exit")
    args = parser.parse_args()

    config_path = Path(args.config)
    state_path = Path(args.state)
    log_path = Path(args.log)
    load_local_env(config_path.with_name(".env"))
    config = load_json(config_path, {})
    poll_seconds = int(config.get("poll_seconds", 300))

    while True:
        run_cycle(config_path, state_path, log_path)
        if args.once:
            return 0
        time.sleep(max(60, poll_seconds))


if __name__ == "__main__":
    raise SystemExit(main())

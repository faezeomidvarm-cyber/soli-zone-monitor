"""Research backtest for the zone-entry variants.

This is a research tool, not a performance promise. Zones are treated as
active for the downloaded period, so the report can contain hindsight bias
unless the user records when each zone became known.
"""

from __future__ import annotations

import argparse
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import pandas as pd
import requests

from zone_monitor import zone_hit


VARIANTS: dict[str, dict[str, Any]] = {
    "fast": {"structure_bars": 1, "reaction": "base"},
    "balanced": {"structure_bars": 2, "reaction": "mid"},
    "conservative": {"structure_bars": 3, "reaction": "edge"},
}


def fetch_yahoo_15m(ticker: str, days: int) -> pd.DataFrame:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
    response = requests.get(
        url,
        params={"interval": "15m", "range": f"{max(2, min(59, days))}d"},
        timeout=20,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    response.raise_for_status()
    result = response.json()["chart"]["result"][0]
    quote = result["indicators"]["quote"][0]
    index = pd.to_datetime(result["timestamp"], unit="s", utc=True).tz_convert(None)
    frame = pd.DataFrame(
        {key: quote[key] for key in ("open", "high", "low", "close")}, index=index
    )
    return frame.dropna().sort_index()


def ohlc_4h(frame: pd.DataFrame) -> pd.DataFrame:
    return frame.resample("4h", origin="start_day").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}
    ).dropna()


def bias_by_bar(frame: pd.DataFrame) -> pd.Series:
    """Return the last fully closed 4H EMA bias for each 15m bar."""
    candles = ohlc_4h(frame)
    close = candles["close"]
    ema20 = close.ewm(span=20, adjust=False).mean()
    ema50 = close.ewm(span=50, adjust=False).mean()
    raw = pd.Series("unknown", index=candles.index, dtype="object")
    raw[(ema20 > ema50) & (close > ema20)] = "bullish"
    raw[(ema20 < ema50) & (close < ema20)] = "bearish"
    # Shift so a 15m bar never uses the still-forming 4H candle.
    return raw.shift(1).reindex(frame.index, method="ffill").fillna("unknown")


def atr(frame: pd.DataFrame, length: int = 14) -> pd.Series:
    previous_close = frame["close"].shift(1)
    true_range = pd.concat(
        [
            frame["high"] - frame["low"],
            (frame["high"] - previous_close).abs(),
            (frame["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return true_range.rolling(length).mean()


def boundaries(zone: dict[str, Any]) -> tuple[float, float, float]:
    line = zone.get("line")
    if line is not None:
        value = float(line)
        return value, value, value
    lower = float(zone["lower"])
    upper = float(zone["upper"])
    return lower, upper, (lower + upper) / 2


def signal_at(
    frame: pd.DataFrame,
    index: int,
    zone: dict[str, Any],
    bias: str,
    variant: dict[str, Any],
) -> bool:
    bars = int(variant["structure_bars"])
    if index < max(20, bars + 1) or bias not in {"bullish", "bearish"}:
        return False
    candle = frame.iloc[index]
    previous = frame.iloc[index - bars : index]
    if not zone_hit(candle, zone):
        return False

    lower, upper, middle = boundaries(zone)
    side = str(zone["side"]).lower()
    close = float(candle["close"])
    open_ = float(candle["open"])
    high = float(candle["high"])
    low = float(candle["low"])
    candle_range = max(high - low, 1e-12)
    bullish_rejection = close > open_ and (close - low) / candle_range >= 0.60
    bearish_rejection = close < open_ and (high - close) / candle_range >= 0.60
    reaction = variant["reaction"]

    if side == "buy":
        level = {"base": lower, "mid": middle, "edge": upper}[reaction]
        return (
            bullish_rejection
            and close >= level
            and close > float(previous["high"].max())
            and bias == "bullish"
        )
    if side == "sell":
        level = {"base": upper, "mid": middle, "edge": lower}[reaction]
        return (
            bearish_rejection
            and close <= level
            and close < float(previous["low"].min())
            and bias == "bearish"
        )
    return False


def simulate_trade(
    frame: pd.DataFrame,
    entry_index: int,
    zone: dict[str, Any],
    rr: float,
    atr_value: float,
) -> dict[str, Any]:
    side = str(zone["side"]).lower()
    lower, upper, _ = boundaries(zone)
    entry = float(frame.iloc[entry_index]["open"])
    buffer = max(float(atr_value) * 0.25, entry * 0.0001)
    if side == "buy":
        stop = lower - buffer
        risk = entry - stop
        target = entry + risk * rr
    else:
        stop = upper + buffer
        risk = stop - entry
        target = entry - risk * rr
    if risk <= 0:
        return {"result": "invalid", "r": 0.0, "bars": 0, "entry": entry}

    for offset, (_, candle) in enumerate(frame.iloc[entry_index:].iterrows()):
        high = float(candle["high"])
        low = float(candle["low"])
        if side == "buy":
            hit_stop = low <= stop
            hit_target = high >= target
            if hit_stop and hit_target:
                return {"result": "stop_first", "r": -1.0, "bars": offset + 1, "entry": entry}
            if hit_stop:
                return {"result": "stop", "r": -1.0, "bars": offset + 1, "entry": entry}
            if hit_target:
                return {"result": "target", "r": rr, "bars": offset + 1, "entry": entry}
        else:
            hit_stop = high >= stop
            hit_target = low <= target
            if hit_stop and hit_target:
                return {"result": "stop_first", "r": -1.0, "bars": offset + 1, "entry": entry}
            if hit_stop:
                return {"result": "stop", "r": -1.0, "bars": offset + 1, "entry": entry}
            if hit_target:
                return {"result": "target", "r": rr, "bars": offset + 1, "entry": entry}

    last = float(frame.iloc[-1]["close"])
    r_value = (last - entry) / risk if side == "buy" else (entry - last) / risk
    return {"result": "open_at_end", "r": r_value, "bars": len(frame) - entry_index, "entry": entry}


def backtest_variant(
    frame: pd.DataFrame,
    zones: list[dict[str, Any]],
    variant: dict[str, Any],
    rr: float,
) -> list[dict[str, Any]]:
    biases = bias_by_bar(frame)
    atr_values = atr(frame)
    trades: list[dict[str, Any]] = []
    next_free = 20
    armed: dict[str, int] = {str(zone["id"]): 0 for zone in zones}

    for index in range(20, len(frame) - 1):
        if index < next_free:
            continue
        for zone in zones:
            zone_id = str(zone["id"])
            if not zone.get("enabled", True):
                continue
            if armed[zone_id] > 0:
                if zone_hit(frame.iloc[index], zone):
                    armed[zone_id] = 3
                else:
                    armed[zone_id] -= 1
                continue
            if not signal_at(frame, index, zone, str(biases.iloc[index]), variant):
                if not zone_hit(frame.iloc[index], zone):
                    armed[zone_id] = max(0, armed[zone_id] - 1)
                else:
                    armed[zone_id] = 3
                continue
            atr_value = float(atr_values.iloc[index]) if pd.notna(atr_values.iloc[index]) else 0.0
            if atr_value <= 0:
                continue
            trade = simulate_trade(frame, index + 1, zone, rr, atr_value)
            trade.update(
                {
                    "zone_id": zone_id,
                    "side": zone["side"],
                    "signal_time": frame.index[index].isoformat(),
                    "rr_target": rr,
                }
            )
            trades.append(trade)
            next_free = index + max(1, int(trade["bars"]))
            armed[zone_id] = 3
            break
    return trades


def summarize(trades: list[dict[str, Any]]) -> dict[str, Any]:
    if not trades:
        return {"trades": 0, "win_rate": 0.0, "total_r": 0.0, "avg_r": 0.0, "max_drawdown_r": 0.0}
    values = [float(trade["r"]) for trade in trades]
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)
    wins = sum(value > 0 for value in values)
    return {
        "trades": len(values),
        "win_rate": round(100 * wins / len(values), 2),
        "total_r": round(sum(values), 3),
        "avg_r": round(sum(values) / len(values), 3),
        "max_drawdown_r": round(abs(max_dd), 3),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(Path(__file__).with_name("zones.json")))
    parser.add_argument("--days", type=int, default=59)
    parser.add_argument("--symbol", action="append", help="limit the run; may be repeated")
    parser.add_argument("--output", default=str(Path(__file__).with_name("backtest_report.json")))
    args = parser.parse_args()

    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    report: dict[str, Any] = {
        "note": "Research only: configured zones are treated as active throughout the downloaded period; SVA is not included.",
        "days": args.days,
        "results": [],
    }
    selected = set(args.symbol or [])
    instruments = [
        item
        for item in config.get("symbols", [])
        if item.get("enabled", True) and (not selected or str(item["id"]) in selected)
    ]

    def download(item: dict[str, Any]) -> tuple[str, pd.DataFrame | None, str | None]:
        symbol = str(item["id"])
        try:
            frame = fetch_yahoo_15m(str(item["data_symbol"]), args.days)
            return symbol, frame.dropna(subset=["open", "high", "low", "close"]), None
        except Exception as exc:
            return symbol, None, str(exc)

    downloaded: dict[str, tuple[pd.DataFrame | None, str | None]] = {}
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(instruments)))) as pool:
        futures = [pool.submit(download, item) for item in instruments]
        for future in as_completed(futures):
            symbol, frame, error = future.result()
            downloaded[symbol] = (frame, error)

    for instrument in instruments:
        symbol = str(instrument["id"])
        frame, error = downloaded.get(symbol, (None, "download did not return"))
        if error or frame is None:
            report["results"].append({"symbol": symbol, "error": error or "no data"})
            continue
        for name, variant in VARIANTS.items():
            for rr in (1.5, 2.0):
                trades = backtest_variant(frame, instrument.get("zones", []), variant, rr)
                report["results"].append(
                    {"symbol": symbol, "variant": name, "rr": rr, **summarize(trades), "trade_log": trades}
                )
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    for result in report["results"]:
        if "error" in result:
            print(f"{result['symbol']}: ERROR {result['error']}")
        else:
            print(
                f"{result['symbol']:8} {result['variant']:13} RR={result['rr']}: "
                f"trades={result['trades']} win={result['win_rate']}% "
                f"totalR={result['total_r']} avgR={result['avg_r']} DD={result['max_drawdown_r']}"
            )
    print(f"report={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Research backtest for the volume-profile strategy.

The script derives the last confirmed 4H swing leg, builds a volume profile
from that leg, treats high-volume nodes as reversal areas, and uses low-volume
nodes as candidate targets. It is deliberately conservative about claims:
Yahoo proxies are not the exact TradingView/OANDA feeds and the profile
approximation assigns each 15m bar's volume to its typical price.
"""

from __future__ import annotations

import argparse
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import pandas as pd
import requests


VARIANTS = {
    "fast": {"structure_bars": 0, "bias": "soft"},
    "balanced": {"structure_bars": 1, "bias": "soft"},
    "conservative": {"structure_bars": 2, "bias": "hard"},
}


def fetch_yahoo(ticker: str, days: int) -> pd.DataFrame:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
    response = requests.get(
        url,
        params={"interval": "15m", "range": f"{max(2, min(59, days))}d"},
        timeout=25,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    response.raise_for_status()
    result = response.json()["chart"]["result"][0]
    quote = result["indicators"]["quote"][0]
    index = pd.to_datetime(result["timestamp"], unit="s", utc=True).tz_convert(None)
    values = {key: quote.get(key) for key in ("open", "high", "low", "close", "volume")}
    frame = pd.DataFrame(values, index=index)
    frame["volume"] = frame["volume"].fillna(0.0)
    return frame.dropna(subset=["open", "high", "low", "close"]).sort_index()


def h4(frame: pd.DataFrame) -> pd.DataFrame:
    return frame.resample("4h", origin="start_day").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    ).dropna()


def atr(frame: pd.DataFrame, length: int = 14) -> pd.Series:
    prev = frame["close"].shift(1)
    tr = pd.concat(
        [frame["high"] - frame["low"], (frame["high"] - prev).abs(), (frame["low"] - prev).abs()],
        axis=1,
    ).max(axis=1)
    return tr.rolling(length).mean()


def biases(frame: pd.DataFrame) -> pd.Series:
    candles = h4(frame)
    close = candles["close"]
    e20 = close.ewm(span=20, adjust=False).mean()
    e50 = close.ewm(span=50, adjust=False).mean()
    values = pd.Series("neutral", index=candles.index, dtype="object")
    values[(e20 > e50) & (close > e20)] = "bullish"
    values[(e20 < e50) & (close < e20)] = "bearish"
    return values.shift(1).reindex(frame.index, method="ffill").fillna("neutral")


def confirmed_pivots(candles: pd.DataFrame, width: int = 3) -> list[tuple[pd.Timestamp, str, float, pd.Timestamp]]:
    pivots: list[tuple[pd.Timestamp, str, float, pd.Timestamp]] = []
    for i in range(width, len(candles) - width):
        row = candles.iloc[i]
        before_after = candles.iloc[i - width : i + width + 1]
        if float(row["high"]) >= float(before_after["high"].max()):
            pivots.append((candles.index[i], "high", float(row["high"]), candles.index[i + width]))
        if float(row["low"]) <= float(before_after["low"].min()):
            pivots.append((candles.index[i], "low", float(row["low"]), candles.index[i + width]))
    pivots.sort(key=lambda item: item[3])
    return pivots


def last_leg(
    candles: pd.DataFrame, pivots: list[tuple[pd.Timestamp, str, float, pd.Timestamp]], now: pd.Timestamp
) -> tuple[pd.Timestamp, pd.Timestamp, str] | None:
    available = [pivot for pivot in pivots if pivot[3] <= now]
    if len(available) < 2:
        return None
    # Keep the latest alternating pair; this is the last confirmed swing leg.
    for right in range(len(available) - 1, 0, -1):
        left = available[right - 1]
        end = available[right]
        if left[1] == end[1] or left[0] >= end[0]:
            continue
        direction = "up" if left[1] == "low" and end[1] == "high" else "down"
        return left[0], end[0], direction
    return None


def profile(segment: pd.DataFrame, bins: int = 24) -> dict[str, Any] | None:
    if len(segment) < 8:
        return None
    lo = float(segment["low"].min())
    hi = float(segment["high"].max())
    step = max((hi - lo) / bins, 1e-12)
    typical = (segment["high"] + segment["low"] + segment["close"]) / 3
    bucket = ((typical - lo) / step).astype(int).clip(0, bins - 1)
    volume = segment["volume"].astype(float)
    if volume.sum() <= 0:
        volume = pd.Series(1.0, index=segment.index)
    histogram = volume.groupby(bucket).sum().reindex(range(bins), fill_value=0.0)
    poc_index = int(histogram.idxmax())
    centers = [lo + (i + 0.5) * step for i in range(bins)]
    q70 = float(histogram.quantile(0.70))
    q30 = float(histogram.quantile(0.30))
    hvn: list[tuple[float, float]] = []
    lvn: list[float] = []
    for i, value in histogram.items():
        left = float(histogram.get(i - 1, value))
        right = float(histogram.get(i + 1, value))
        if value >= q70 and value >= left and value >= right:
            hvn.append((centers[int(i)] - step / 2, centers[int(i)] + step / 2))
        if value <= q30 and value <= left and value <= right:
            lvn.append(centers[int(i)])
    poc = centers[poc_index]
    if not hvn:
        hvn = [(poc - step / 2, poc + step / 2)]
    return {"hvn": hvn, "lvn": lvn, "poc": poc, "step": step}


def rejection_signal(
    frame: pd.DataFrame,
    index: int,
    zone: tuple[float, float],
    side: str,
    structure_bars: int,
    bias: str,
    bias_mode: str,
) -> bool:
    if index < max(20, structure_bars + 1):
        return False
    candle = frame.iloc[index]
    previous = frame.iloc[index - structure_bars : index] if structure_bars else frame.iloc[index:index]
    low, high = zone
    close = float(candle["close"])
    open_ = float(candle["open"])
    candle_low = float(candle["low"])
    candle_high = float(candle["high"])
    span = max(candle_high - candle_low, 1e-12)
    bullish = close > open_ and (close - candle_low) / span >= 0.60
    bearish = close < open_ and (candle_high - close) / span >= 0.60
    if side == "buy":
        if not bullish or close < (low + high) / 2:
            return False
        if structure_bars and close <= float(previous["high"].max()):
            return False
        return bias_mode == "soft" or bias == "bullish"
    if side == "sell":
        if not bearish or close > (low + high) / 2:
            return False
        if structure_bars and close >= float(previous["low"].min()):
            return False
        return bias_mode == "soft" or bias == "bearish"
    return False


def simulate(
    frame: pd.DataFrame,
    entry_index: int,
    side: str,
    zone: tuple[float, float],
    targets: list[float],
    rr: float,
    atr_value: float,
) -> dict[str, Any]:
    entry = float(frame.iloc[entry_index]["open"])
    low, high = zone
    buffer = max(atr_value * 0.25, entry * 0.0001)
    if side == "buy":
        stop = low - buffer
        risk = entry - stop
        candidates = [target for target in targets if target > entry]
        target = min(candidates) if candidates else entry + risk * rr
    else:
        stop = high + buffer
        risk = stop - entry
        candidates = [target for target in targets if target < entry]
        target = max(candidates) if candidates else entry - risk * rr
    if risk <= 0:
        return {"r": 0.0, "result": "invalid", "bars": 0}
    # Do not accept a volume target that is closer than 1R.
    if side == "buy" and target < entry + risk:
        target = entry + risk * rr
    if side == "sell" and target > entry - risk:
        target = entry - risk * rr
    for offset, (_, candle) in enumerate(frame.iloc[entry_index:].iterrows()):
        high_price = float(candle["high"])
        low_price = float(candle["low"])
        if side == "buy":
            hit_stop, hit_target = low_price <= stop, high_price >= target
            if hit_stop and hit_target:
                return {"r": -1.0, "result": "stop_first", "bars": offset + 1}
            if hit_stop:
                return {"r": -1.0, "result": "stop", "bars": offset + 1}
            if hit_target:
                return {"r": round((target - entry) / risk, 4), "result": "target", "bars": offset + 1}
        else:
            hit_stop, hit_target = high_price >= stop, low_price <= target
            if hit_stop and hit_target:
                return {"r": -1.0, "result": "stop_first", "bars": offset + 1}
            if hit_stop:
                return {"r": -1.0, "result": "stop", "bars": offset + 1}
            if hit_target:
                return {"r": round((entry - target) / risk, 4), "result": "target", "bars": offset + 1}
    return {"r": 0.0, "result": "open_at_end", "bars": len(frame) - entry_index}


def run_symbol(frame: pd.DataFrame, variant: dict[str, Any], rr: float) -> list[dict[str, Any]]:
    candles = h4(frame)
    pivots = confirmed_pivots(candles)
    bias = biases(frame)
    atr_values = atr(frame)
    trades: list[dict[str, Any]] = []
    next_free = 20
    last_leg_key: tuple[pd.Timestamp, pd.Timestamp] | None = None
    fixed_profile: dict[str, Any] | None = None
    for i in range(20, len(frame) - 1):
        if i < next_free:
            continue
        leg = last_leg(candles, pivots, frame.index[i])
        if leg is None:
            continue
        start, end, _ = leg
        if last_leg_key != (start, end):
            segment = frame.loc[(frame.index >= start) & (frame.index <= end)]
            fixed_profile = profile(segment)
            last_leg_key = (start, end)
        if not fixed_profile:
            continue
        candle = frame.iloc[i]
        close = float(candle["close"])
        previous_close = float(frame.iloc[i - 1]["close"])
        for zone in fixed_profile["hvn"]:
            low, high = zone
            if not (float(candle["low"]) <= high and float(candle["high"]) >= low):
                continue
            if float(atr_values.iloc[i]) <= 0 or pd.isna(atr_values.iloc[i]):
                continue
            side = "buy" if previous_close < low else "sell" if previous_close > high else ""
            if not side:
                continue
            if not rejection_signal(frame, i, zone, side, int(variant["structure_bars"]), str(bias.iloc[i]), variant["bias"]):
                continue
            entry_index = i + 1
            trade = simulate(frame, entry_index, side, zone, fixed_profile["lvn"], rr, float(atr_values.iloc[i]))
            trade.update({"side": side, "signal_time": frame.index[i].isoformat(), "poc": fixed_profile["poc"]})
            trades.append(trade)
            next_free = i + max(1, int(trade["bars"]))
            break
    return trades


def summary(trades: list[dict[str, Any]]) -> dict[str, Any]:
    values = [float(item["r"]) for item in trades]
    if not values:
        return {"trades": 0, "win_rate": 0.0, "total_r": 0.0, "avg_r": 0.0, "max_dd_r": 0.0}
    equity = peak = 0.0
    max_dd = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)
    return {
        "trades": len(values),
        "win_rate": round(100 * sum(value > 0 for value in values) / len(values), 2),
        "total_r": round(sum(values), 3),
        "avg_r": round(sum(values) / len(values), 3),
        "max_dd_r": round(abs(max_dd), 3),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--universe", default=str(Path(__file__).with_name("backtest_universe.json")))
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--output", default=str(Path(__file__).with_name("volume_profile_report.json")))
    args = parser.parse_args()
    universe = json.loads(Path(args.universe).read_text(encoding="utf-8"))
    instruments = universe.get("watchlist", []) + universe.get("redlist", [])

    def download(item: dict[str, Any]) -> tuple[str, pd.DataFrame | None, str | None]:
        try:
            return item["id"], fetch_yahoo(item["data_symbol"], args.days), None
        except Exception as exc:
            return item["id"], None, str(exc)

    data: dict[str, tuple[pd.DataFrame | None, str | None]] = {}
    with ThreadPoolExecutor(max_workers=min(12, max(1, len(instruments)))) as pool:
        for future in as_completed([pool.submit(download, item) for item in instruments]):
            symbol, frame, error = future.result()
            data[symbol] = frame, error

    report: dict[str, Any] = {
        "note": "Research only. Volume is approximate Yahoo proxy volume; SVA is not included.",
        "days": args.days,
        "results": [],
    }
    for item in instruments:
        symbol = item["id"]
        frame, error = data.get(symbol, (None, "no result"))
        if error or frame is None:
            report["results"].append({"symbol": symbol, "error": error or "no data"})
            continue
        for name, variant in VARIANTS.items():
            for rr in (1.5, 2.0):
                trades = run_symbol(frame, variant, rr)
                report["results"].append({"symbol": symbol, "variant": name, "rr": rr, **summary(trades)})
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    for result in report["results"]:
        if "error" in result:
            print(f"{result['symbol']:10} ERROR {result['error']}")
        else:
            print(
                f"{result['symbol']:10} {result['variant']:13} RR={result['rr']}: "
                f"trades={result['trades']} win={result['win_rate']}% "
                f"totalR={result['total_r']} avgR={result['avg_r']} DD={result['max_dd_r']}"
            )
    print(f"report={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

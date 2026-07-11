"""
EMA/ADX signal calculation and the stop/trail state machine.
No MetaTrader5 dependency — fully unit-testable in isolation.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional

import pandas as pd

log = logging.getLogger(__name__)


class Direction(str, Enum):
    BUY = "buy"
    SELL = "sell"


@dataclass
class PositionState:
    direction: Direction
    entry_price: float
    initial_stop: float
    stop_level: float
    adx_at_entry: float
    entry_time: datetime
    max_favorable_excursion: float = field(default=0.0)


# ── Indicators ────────────────────────────────────────────────

def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def _adx(df: pd.DataFrame, period: int) -> pd.Series:
    """Wilder ADX from High / Low / Close columns."""
    high, low, close = df["High"], df["Low"], df["Close"]

    up = high.diff()
    down = -low.diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)

    tr = pd.concat(
        [high - low, (high - close.shift()).abs(), (low - close.shift()).abs()],
        axis=1,
    ).max(axis=1)

    alpha = 1.0 / period
    atr = tr.ewm(alpha=alpha, adjust=False).mean()
    plus_di = 100 * (plus_dm.ewm(alpha=alpha, adjust=False).mean() / atr)
    minus_di = 100 * (minus_dm.ewm(alpha=alpha, adjust=False).mean() / atr)
    dx = (100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, float("nan")))
    return dx.ewm(alpha=alpha, adjust=False).mean().fillna(0)


# ── Signal calculation ────────────────────────────────────────

def compute_signals(
    rates: list,
    ema_fast: int,
    ema_slow: int,
    adx_period: int,
) -> dict:
    """
    Compute EMA crossover and ADX from MT5 rate dicts.
    Drops the current incomplete bar (last element) before analysis.
    Each dict must have lowercase keys: open, high, low, close, time.

    Returns signal dict for the most recently *closed* bar.
    """
    # Drop current open (incomplete) bar — use only completed bars
    bars = rates[:-1] if len(rates) > 1 else rates

    df = pd.DataFrame(bars)
    df = df.rename(columns={"open": "Open", "high": "High", "low": "Low", "close": "Close"})
    df = df[["Open", "High", "Low", "Close"]].astype(float)

    null_result = {"bull_cross": False, "bear_cross": False, "adx": 0.0,
                   "close": float(df["Close"].iloc[-1])}

    if len(df) < max(ema_slow, adx_period) + 2:
        log.warning("Not enough bars to compute signals (%d bars)", len(df))
        return null_result

    fast = _ema(df["Close"], ema_fast)
    slow = _ema(df["Close"], ema_slow)
    adx_series = _adx(df, adx_period)

    diff = fast - slow
    bull_cross = bool(diff.iloc[-2] <= 0 and diff.iloc[-1] > 0)
    bear_cross = bool(diff.iloc[-2] >= 0 and diff.iloc[-1] < 0)

    # Bullish Pullback
    bull_trend = bool(diff.iloc[-1] > 0 and diff.iloc[-2] > 0)
    bull_touch = bool((df["Low"].iloc[-1] <= fast.iloc[-1]) or (df["Low"].iloc[-2] <= fast.iloc[-2]))
    bull_bounce = bool((df["Close"].iloc[-1] > df["Open"].iloc[-1]) and (df["Close"].iloc[-1] > fast.iloc[-1]))
    bull_pullback = bull_trend and bull_touch and bull_bounce

    # Bearish Pullback
    bear_trend = bool(diff.iloc[-1] < 0 and diff.iloc[-2] < 0)
    bear_touch = bool((df["High"].iloc[-1] >= fast.iloc[-1]) or (df["High"].iloc[-2] >= fast.iloc[-2]))
    bear_bounce = bool((df["Close"].iloc[-1] < df["Open"].iloc[-1]) and (df["Close"].iloc[-1] < fast.iloc[-1]))
    bear_pullback = bear_trend and bear_touch and bear_bounce

    return {
        "bull_cross": bull_cross,
        "bear_cross": bear_cross,
        "bull_pullback": bull_pullback,
        "bear_pullback": bear_pullback,
        "adx": float(adx_series.iloc[-1]),
        "ema_fast": float(fast.iloc[-1]),
        "ema_slow": float(slow.iloc[-1]),
        "close": float(df["Close"].iloc[-1]),
        "time": int(bars[-1]["time"]) if len(bars) > 0 else 0,
    }


def get_htf_trend(rates: list, ema_period: int) -> Optional[Direction]:
    """
    Computes the HTF trend based on Close relative to EMA.
    Returns Direction.BUY if Close > EMA, Direction.SELL if Close < EMA.
    """
    bars = rates[:-1] if len(rates) > 1 else rates
    if len(bars) < ema_period + 1:
        return None
        
    df = pd.DataFrame(bars)
    df = df.rename(columns={"close": "Close"}).astype(float)
    
    ema = _ema(df["Close"], ema_period)
    last_close = float(df["Close"].iloc[-1])
    last_ema = float(ema.iloc[-1])
    
    if last_close > last_ema:
        return Direction.BUY
    elif last_close < last_ema:
        return Direction.SELL
    return None


# ── Stop / trail state machine ────────────────────────────────

def update_trailing_stop(
    pos: PositionState,
    bid: float,
    ask: float,
    trail_pips: float,
    pip_size: float,
) -> tuple[PositionState, bool]:
    """
    Apply the stop / trail rule to an open position against current bid/ask.

    Rules (exact per spec):
    - While not in profit: stop stays fixed at initial_stop. Never widens.
    - Once price moves into profit: stop trails at trail_pips distance from
      the most favorable price seen. Only ever tightens, never loosens.
    - Buy positions: compare stop against bid (what the broker pays us to close).
    - Sell positions: compare stop against ask (what we pay broker to close).

    Returns (updated_pos, stop_was_hit_in_software).
    Note: MT5 manages the SL server-side; this is our local tracking copy.
    The bot detects the actual close by checking whether the position still
    exists in MT5 — not by trusting this boolean for final exit logic.
    """
    trail_dist = trail_pips * pip_size

    if pos.direction == Direction.BUY:
        if bid <= pos.stop_level:
            return pos, True
        favorable = bid - pos.entry_price
        if favorable > pos.max_favorable_excursion:
            pos.max_favorable_excursion = favorable
        if favorable > 0:
            candidate = bid - trail_dist
            if candidate > pos.stop_level:
                pos.stop_level = candidate
    else:
        if ask >= pos.stop_level:
            return pos, True
        favorable = pos.entry_price - ask
        if favorable > pos.max_favorable_excursion:
            pos.max_favorable_excursion = favorable
        if favorable > 0:
            candidate = ask + trail_dist
            if candidate < pos.stop_level:
                pos.stop_level = candidate

    return pos, False


def classify_exit(pos: PositionState) -> str:
    if pos.direction == Direction.BUY:
        return "trailing_stop" if pos.stop_level > pos.initial_stop else "initial_stop"
    return "trailing_stop" if pos.stop_level < pos.initial_stop else "initial_stop"


def calc_pips(pos: PositionState, exit_price: float, pip_size: float) -> float:
    if pos.direction == Direction.BUY:
        return (exit_price - pos.entry_price) / pip_size
    return (pos.entry_price - exit_price) / pip_size

"""
K9 Smart Grid Strategy Engine
==============================

Pure strategy logic for the grid trading bot.
No MetaTrader5 dependency — fully unit-testable in isolation.

Market Regime Detection:
  - ADX < range_threshold  →  RANGING   →  full grid active
  - ADX > trend_threshold  →  TRENDING  →  grid pauses (all pending cancelled)
  - ADX in between          →  NEUTRAL   →  reduced grid (fewer levels, wider spacing)

Grid Spacing:
  - Based on ATR × multiplier, clamped between min/max pip settings
  - Low volatility  → tighter grid (more trades, smaller moves)
  - High volatility → wider grid (fewer trades, bigger moves)

Grid Levels:
  - BUY LIMIT orders placed below current price
  - SELL LIMIT orders placed above current price
  - Each level has a fixed TP at grid_spacing distance
  - When a level's TP is hit, the level is replenished (new limit order)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional

import pandas as pd

log = logging.getLogger(__name__)


# ── Market Regime ─────────────────────────────────────────────

class MarketRegime(str, Enum):
    RANGING = "ranging"       # ADX below range threshold — full grid
    NEUTRAL = "neutral"       # ADX between thresholds — reduced grid
    TRENDING = "trending"     # ADX above trend threshold — grid paused


def detect_regime(
    adx: float,
    range_threshold: float = 20.0,
    trend_threshold: float = 25.0,
) -> MarketRegime:
    """
    Classify market regime based on ADX value.

    Args:
        adx: Current ADX value
        range_threshold: Below this → RANGING (grid fully active)
        trend_threshold: Above this → TRENDING (grid paused)

    Returns:
        MarketRegime enum value
    """
    if adx < range_threshold:
        return MarketRegime.RANGING
    if adx > trend_threshold:
        return MarketRegime.TRENDING
    return MarketRegime.NEUTRAL


# ── Grid Spacing ──────────────────────────────────────────────

def calculate_grid_spacing(
    atr_value: float,
    pip_size: float,
    multiplier: float = 1.0,
    min_spacing_pips: float = 5.0,
    max_spacing_pips: float = 10.0,
) -> float:
    """
    Calculate dynamic grid spacing based on ATR.

    The raw spacing is ATR × multiplier, then clamped to [min, max] in pips.
    Returns spacing in price units (not pips).

    Args:
        atr_value: Current ATR value in price units
        pip_size: Size of one pip in price units (e.g., 0.01 for XAUUSD)
        multiplier: ATR multiplier (default 1.0)
        min_spacing_pips: Minimum spacing in pips
        max_spacing_pips: Maximum spacing in pips

    Returns:
        Grid spacing in price units
    """
    raw_spacing = atr_value * multiplier
    raw_pips = raw_spacing / pip_size

    # Clamp to [min, max] pips
    clamped_pips = max(min_spacing_pips, min(raw_pips, max_spacing_pips))

    spacing = clamped_pips * pip_size
    log.debug(
        "Grid spacing: ATR=%.5f raw_pips=%.1f → clamped=%.1f pips → spacing=%.5f",
        atr_value, raw_pips, clamped_pips, spacing,
    )
    return spacing


# ── ATR Calculation ───────────────────────────────────────────

def compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """
    Standard ATR (Average True Range) calculation.

    Args:
        df: DataFrame with columns High, Low, Close
        period: ATR lookback period

    Returns:
        ATR series
    """
    high, low, close = df["High"], df["Low"], df["Close"]

    tr = pd.concat(
        [high - low, (high - close.shift()).abs(), (low - close.shift()).abs()],
        axis=1,
    ).max(axis=1)

    alpha = 1.0 / period
    return tr.ewm(alpha=alpha, adjust=False).mean()


def compute_adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Standard Wilder ADX from High / Low / Close columns."""
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


def compute_indicators(
    rates: list,
    atr_period: int = 14,
    adx_period: int = 14,
) -> dict:
    """
    Compute ATR and ADX from MT5 rate dicts.
    Drops the current incomplete bar (last element) before analysis.

    Args:
        rates: List of rate dicts from MT5 (keys: open, high, low, close, time)
        atr_period: ATR lookback period
        adx_period: ADX lookback period

    Returns:
        Dict with keys: atr, adx, close
    """
    # Drop current open (incomplete) bar
    bars = rates[:-1] if len(rates) > 1 else rates

    df = pd.DataFrame(bars)
    df = df.rename(columns={"open": "Open", "high": "High", "low": "Low", "close": "Close"})
    df = df[["Open", "High", "Low", "Close"]].astype(float)

    min_bars = max(atr_period, adx_period) + 2
    if len(df) < min_bars:
        log.warning("Not enough bars to compute indicators (%d bars, need %d)", len(df), min_bars)
        return {"atr": 0.0, "adx": 0.0, "close": float(df["Close"].iloc[-1])}

    atr_series = compute_atr(df, atr_period)
    adx_series = compute_adx(df, adx_period)

    return {
        "atr": float(atr_series.iloc[-1]),
        "adx": float(adx_series.iloc[-1]),
        "close": float(df["Close"].iloc[-1]),
    }


# ── Grid Level ────────────────────────────────────────────────

class GridLevelStatus(str, Enum):
    PENDING = "pending"       # Limit order placed, waiting for fill
    FILLED = "filled"         # Limit order filled, position open, waiting for TP
    CLOSED = "closed"         # Position closed (TP hit or manually closed)
    CANCELLED = "cancelled"   # Limit order was cancelled


@dataclass
class GridLevel:
    """Represents a single level in the grid."""
    direction: str            # 'buy' or 'sell'
    order_price: float        # Price of the limit order
    tp_price: float           # Take-profit price
    lot_size: float           # Volume
    status: GridLevelStatus = GridLevelStatus.PENDING
    order_ticket: Optional[int] = None   # MT5 pending order ticket
    position_ticket: Optional[int] = None  # MT5 position ticket (after fill)
    fill_price: Optional[float] = None   # Actual fill price
    fill_time: Optional[datetime] = None
    close_price: Optional[float] = None
    close_time: Optional[datetime] = None
    pips: Optional[float] = None


# ── Grid Generation ───────────────────────────────────────────

def generate_grid(
    center_price: float,
    spacing: float,
    levels_above: int,
    levels_below: int,
    tp_pips: float,
    pip_size: float,
    lot_size: float,
    digits: int = 2,
) -> list[GridLevel]:
    """
    Generate a full grid of levels above and below the center price.

    - Levels ABOVE center → SELL LIMIT orders (sell high, TP lower)
    - Levels BELOW center → BUY LIMIT orders (buy low, TP higher)

    Args:
        center_price: Current price to center the grid around
        spacing: Distance between grid levels in price units
        levels_above: Number of SELL LIMIT levels above center
        levels_below: Number of BUY LIMIT levels below center
        tp_pips: Take-profit distance in pips per level
        pip_size: Pip size in price units
        lot_size: Volume per grid level
        digits: Price rounding precision

    Returns:
        List of GridLevel objects
    """
    tp_dist = tp_pips * pip_size
    levels = []

    # SELL LIMIT levels above center price
    for i in range(1, levels_above + 1):
        price = round(center_price + i * spacing, digits)
        tp = round(price - tp_dist, digits)
        levels.append(GridLevel(
            direction="sell",
            order_price=price,
            tp_price=tp,
            lot_size=lot_size,
        ))

    # BUY LIMIT levels below center price
    for i in range(1, levels_below + 1):
        price = round(center_price - i * spacing, digits)
        tp = round(price + tp_dist, digits)
        levels.append(GridLevel(
            direction="buy",
            order_price=price,
            tp_price=tp,
            lot_size=lot_size,
        ))

    log.info(
        "Generated grid: center=%.2f spacing=%.5f levels=%d (sell=%d buy=%d) TP=%.1f pips",
        center_price, spacing, len(levels), levels_above, levels_below, tp_pips,
    )
    return levels


def should_recenter_grid(
    current_price: float,
    grid_center: float,
    spacing: float,
    threshold_levels: int = 3,
) -> bool:
    """
    Check whether the price has drifted far enough from grid center
    to warrant rebuilding the grid.

    Args:
        current_price: Current market price
        grid_center: The price the grid was originally centered on
        spacing: Grid spacing in price units
        threshold_levels: Number of levels of drift before recentering

    Returns:
        True if grid should be recentered
    """
    drift = abs(current_price - grid_center)
    threshold = threshold_levels * spacing
    if drift >= threshold:
        log.info(
            "Grid recenter needed: price=%.2f center=%.2f drift=%.5f threshold=%.5f",
            current_price, grid_center, drift, threshold,
        )
        return True
    return False


def levels_for_regime(
    regime: MarketRegime,
    full_levels_above: int,
    full_levels_below: int,
) -> tuple[int, int]:
    """
    Adjust grid level counts based on market regime.

    - RANGING: full grid
    - NEUTRAL: half grid (rounded up)
    - TRENDING: no grid

    Args:
        regime: Current market regime
        full_levels_above: Configured levels above center
        full_levels_below: Configured levels below center

    Returns:
        (adjusted_above, adjusted_below)
    """
    if regime == MarketRegime.RANGING:
        return full_levels_above, full_levels_below
    if regime == MarketRegime.NEUTRAL:
        # Half grid, minimum 1 level each side
        return max(1, full_levels_above // 2), max(1, full_levels_below // 2)
    # TRENDING — no grid
    return 0, 0

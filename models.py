from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class TradeRecord(BaseModel):
    id: Optional[int] = None
    direction: str                  # 'buy' or 'sell'
    entry_time: datetime
    exit_time: Optional[datetime] = None
    entry_price: float
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None   # 'initial_stop' | 'trailing_stop'
    pips: Optional[float] = None
    adx_at_entry: float
    lot_size: float
    running_equity: Optional[float] = None


class BotConfig(BaseModel):
    symbol: str
    pip_size: float
    adx_threshold: float
    adx_threshold_min: float
    adx_threshold_max: float
    initial_stop_pips: float
    trail_pips: float
    lot_size: float
    ema_fast: int
    ema_slow: int
    adx_period: int
    timeframe: str
    poll_interval_seconds: int
    bars_to_fetch: int
    max_concurrent_trades: int
    tuning_review_every_n_trades: int
    adx_tune_step: float
    auto_apply_tuning: bool
    max_slippage_points: int
    htf_filter_enabled: bool
    htf_timeframe: str
    htf_ema_period: int


class TuningSuggestion(BaseModel):
    id: Optional[int] = None
    created_at: datetime
    current_threshold: float
    suggested_threshold: float
    reasoning: str
    applied: bool = False
    applied_at: Optional[datetime] = None


class BotStatus(BaseModel):
    running: bool
    active_positions: int
    current_adx_threshold: float
    total_closed_trades: int
    account_equity: Optional[float] = None
    account_balance: Optional[float] = None
    account_type: Optional[str] = None     # 'demo' | 'real'
    error: Optional[str] = None


# ── Grid Bot Models ──────────────────────────────────────────

class GridConfig(BaseModel):
    """Configuration for the grid trading bot."""
    symbol: str
    pip_size: float
    lot_size: float
    levels_above: int                  # Number of SELL LIMIT levels above center
    levels_below: int                  # Number of BUY LIMIT levels below center
    tp_pips: float                     # Take-profit per grid level (in pips)
    min_spacing_pips: float            # Minimum grid spacing (pips)
    max_spacing_pips: float            # Maximum grid spacing (pips)
    atr_multiplier: float              # ATR multiplier for spacing calculation
    atr_period: int                    # ATR lookback period
    adx_period: int                    # ADX lookback period
    adx_range_threshold: float         # Below this = ranging (grid active)
    adx_trend_threshold: float         # Above this = trending (grid paused)
    kill_switch_pct: float             # Max equity drawdown % before emergency close
    poll_interval_seconds: int         # Tick polling interval
    bars_to_fetch: int                 # OHLCV history depth for indicators
    timeframe: str                     # MT5 timeframe for indicator calculation
    recenter_levels: int               # Recenter grid if price drifts this many levels
    max_slippage_points: int           # Max slippage for order execution


class GridStatus(BaseModel):
    """Status response for the grid bot /grid/status endpoint."""
    running: bool
    grid_active: bool
    regime: str                        # 'ranging' | 'neutral' | 'trending'
    center_price: float
    spacing: float
    pending_orders: int
    open_positions: int
    total_realized_pips: float
    total_realized_trades: int
    win_rate: float
    kill_switch_triggered: bool
    account_equity: Optional[float] = None
    account_balance: Optional[float] = None
    account_type: Optional[str] = None
    error: Optional[str] = None


class GridTradeRecord(BaseModel):
    """A single grid trade record for persistence."""
    id: Optional[int] = None
    direction: str                     # 'buy' or 'sell'
    entry_time: datetime
    exit_time: Optional[datetime] = None
    entry_price: float
    exit_price: Optional[float] = None
    tp_price: float
    pips: Optional[float] = None
    lot_size: float
    running_equity: Optional[float] = None


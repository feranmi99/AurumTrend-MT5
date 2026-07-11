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

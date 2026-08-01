"""
K9 Grid State Manager
=====================

Tracks all grid levels, filled positions, realized/unrealized P&L,
and provides the equity kill-switch logic.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from grid_strategy import GridLevel, GridLevelStatus, MarketRegime

log = logging.getLogger(__name__)


@dataclass
class GridState:
    """
    Mutable state container for the grid bot.

    Tracks the current grid configuration, all active levels,
    and cumulative performance metrics.
    """
    # Grid metadata
    center_price: float = 0.0
    spacing: float = 0.0
    regime: MarketRegime = MarketRegime.RANGING

    # Active grid levels (keyed by order_ticket for pending, position_ticket for filled)
    levels: list[GridLevel] = field(default_factory=list)

    # Performance tracking
    total_realized_pips: float = 0.0
    total_realized_trades: int = 0
    total_winning_trades: int = 0
    total_losing_trades: int = 0
    session_start_equity: Optional[float] = None

    # Control flags
    grid_active: bool = False
    kill_switch_triggered: bool = False
    last_regime_change: Optional[datetime] = None

    # ── Level management ──────────────────────────────────────

    @property
    def pending_levels(self) -> list[GridLevel]:
        """All levels with pending limit orders."""
        return [lv for lv in self.levels if lv.status == GridLevelStatus.PENDING]

    @property
    def filled_levels(self) -> list[GridLevel]:
        """All levels with open positions (filled, waiting for TP)."""
        return [lv for lv in self.levels if lv.status == GridLevelStatus.FILLED]

    @property
    def pending_count(self) -> int:
        return len(self.pending_levels)

    @property
    def filled_count(self) -> int:
        return len(self.filled_levels)

    def find_level_by_order(self, order_ticket: int) -> Optional[GridLevel]:
        """Find a grid level by its pending order ticket."""
        for lv in self.levels:
            if lv.order_ticket == order_ticket:
                return lv
        return None

    def find_level_by_position(self, position_ticket: int) -> Optional[GridLevel]:
        """Find a grid level by its position ticket."""
        for lv in self.levels:
            if lv.position_ticket == position_ticket:
                return lv
        return None

    def mark_filled(
        self,
        order_ticket: int,
        position_ticket: int,
        fill_price: float,
        fill_time: Optional[datetime] = None,
    ) -> Optional[GridLevel]:
        """
        Mark a pending level as filled (limit order triggered).

        Args:
            order_ticket: The pending order ticket that was filled
            position_ticket: The resulting position ticket
            fill_price: Actual fill price
            fill_time: Time of fill

        Returns:
            The updated GridLevel, or None if not found
        """
        lv = self.find_level_by_order(order_ticket)
        if lv is None:
            log.warning("mark_filled: order_ticket=%d not found in grid levels", order_ticket)
            return None

        lv.status = GridLevelStatus.FILLED
        lv.position_ticket = position_ticket
        lv.fill_price = fill_price
        lv.fill_time = fill_time or datetime.utcnow()

        log.info(
            "Grid level FILLED: %s @ %.2f (order=%d → pos=%d)",
            lv.direction, fill_price, order_ticket, position_ticket,
        )
        return lv

    def mark_closed(
        self,
        position_ticket: int,
        close_price: float,
        pip_size: float,
        close_time: Optional[datetime] = None,
    ) -> Optional[GridLevel]:
        """
        Mark a filled level as closed (TP hit or manual close).

        Args:
            position_ticket: The position ticket that was closed
            close_price: Actual close price
            pip_size: Pip size for P&L calculation
            close_time: Time of close

        Returns:
            The updated GridLevel, or None if not found
        """
        lv = self.find_level_by_position(position_ticket)
        if lv is None:
            log.warning("mark_closed: position_ticket=%d not found in grid levels", position_ticket)
            return None

        lv.status = GridLevelStatus.CLOSED
        lv.close_price = close_price
        lv.close_time = close_time or datetime.utcnow()

        # Calculate pips
        if lv.fill_price is not None:
            if lv.direction == "buy":
                lv.pips = round((close_price - lv.fill_price) / pip_size, 2)
            else:
                lv.pips = round((lv.fill_price - close_price) / pip_size, 2)
        else:
            lv.pips = 0.0

        # Update running totals
        self.total_realized_pips += lv.pips
        self.total_realized_trades += 1
        if lv.pips > 0:
            self.total_winning_trades += 1
        else:
            self.total_losing_trades += 1

        log.info(
            "Grid level CLOSED: %s pos=%d  %+.1f pips (total realized: %+.1f pips, %d trades)",
            lv.direction, position_ticket, lv.pips,
            self.total_realized_pips, self.total_realized_trades,
        )
        return lv

    def remove_closed_and_cancelled(self) -> list[GridLevel]:
        """
        Remove all closed/cancelled levels from the active list.
        Returns the removed levels for logging.
        """
        removed = [lv for lv in self.levels
                    if lv.status in (GridLevelStatus.CLOSED, GridLevelStatus.CANCELLED)]
        self.levels = [lv for lv in self.levels
                       if lv.status not in (GridLevelStatus.CLOSED, GridLevelStatus.CANCELLED)]
        return removed

    def cancel_all_pending(self) -> list[GridLevel]:
        """
        Mark all pending levels as cancelled.
        Returns the levels that were cancelled (caller should cancel in MT5).
        """
        cancelled = []
        for lv in self.levels:
            if lv.status == GridLevelStatus.PENDING:
                lv.status = GridLevelStatus.CANCELLED
                cancelled.append(lv)
        return cancelled

    def clear_grid(self) -> None:
        """Remove all levels from the grid."""
        self.levels.clear()
        self.center_price = 0.0
        self.spacing = 0.0

    # ── Kill switch ───────────────────────────────────────────

    def check_kill_switch(
        self,
        current_equity: float,
        kill_switch_pct: float,
    ) -> bool:
        """
        Check if equity drawdown has exceeded the kill-switch threshold.

        Args:
            current_equity: Current account equity
            kill_switch_pct: Maximum allowed drawdown percentage (e.g., 10.0 for 10%)

        Returns:
            True if kill switch should be triggered
        """
        if self.session_start_equity is None or self.session_start_equity <= 0:
            return False

        drawdown_pct = ((self.session_start_equity - current_equity) / self.session_start_equity) * 100

        if drawdown_pct >= kill_switch_pct:
            log.critical(
                "⛔ KILL SWITCH TRIGGERED: equity=%.2f start=%.2f drawdown=%.1f%% (limit=%.1f%%)",
                current_equity, self.session_start_equity, drawdown_pct, kill_switch_pct,
            )
            self.kill_switch_triggered = True
            return True

        return False

    # ── Unrealized P&L ────────────────────────────────────────

    def compute_unrealized_pnl(self, bid: float, ask: float, pip_size: float) -> float:
        """
        Compute total unrealized P&L across all filled (open) positions.

        Args:
            bid: Current bid price
            ask: Current ask price
            pip_size: Pip size in price units

        Returns:
            Total unrealized P&L in pips
        """
        total = 0.0
        for lv in self.filled_levels:
            if lv.fill_price is None:
                continue
            if lv.direction == "buy":
                total += (bid - lv.fill_price) / pip_size
            else:
                total += (lv.fill_price - ask) / pip_size
        return round(total, 2)

    # ── Summary ───────────────────────────────────────────────

    def summary(self) -> dict:
        """Return a summary dict for API/logging."""
        return {
            "grid_active": self.grid_active,
            "regime": self.regime.value,
            "center_price": self.center_price,
            "spacing": self.spacing,
            "pending_orders": self.pending_count,
            "open_positions": self.filled_count,
            "total_levels": len(self.levels),
            "total_realized_pips": round(self.total_realized_pips, 2),
            "total_realized_trades": self.total_realized_trades,
            "win_rate": (
                round(100 * self.total_winning_trades / self.total_realized_trades, 1)
                if self.total_realized_trades > 0 else 0.0
            ),
            "kill_switch_triggered": self.kill_switch_triggered,
        }

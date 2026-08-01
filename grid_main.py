"""
K9 Grid Bot — FastAPI service
==============================

Smart grid trading bot for XAUUSD.
Runs alongside the K9 trend bot as a separate service on a different port.

Start:  uvicorn grid_main:app --host 0.0.0.0 --port 8001
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException

import trade_log
from grid_state import GridState
from grid_strategy import (
    GridLevel,
    GridLevelStatus,
    MarketRegime,
    calculate_grid_spacing,
    compute_indicators,
    detect_regime,
    generate_grid,
    levels_for_regime,
    should_recenter_grid,
)
from models import GridConfig, GridStatus, GridTradeRecord
from mt5_client import MT5Client, MT5Error

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("k9grid.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("k9grid")


# ── Config ───────────────────────────────────────────────────

def _load_grid_config() -> GridConfig:
    return GridConfig(
        symbol=os.environ["SYMBOL"],
        pip_size=float(os.environ.get("PIP_SIZE", "0.01")),
        lot_size=float(os.environ.get("GRID_LOT_SIZE", "0.01")),
        levels_above=int(os.environ.get("GRID_LEVELS_ABOVE", "5")),
        levels_below=int(os.environ.get("GRID_LEVELS_BELOW", "5")),
        tp_pips=float(os.environ.get("GRID_TP_PIPS", "7")),
        min_spacing_pips=float(os.environ.get("GRID_MIN_SPACING_PIPS", "5")),
        max_spacing_pips=float(os.environ.get("GRID_MAX_SPACING_PIPS", "10")),
        atr_multiplier=float(os.environ.get("GRID_ATR_MULTIPLIER", "1.0")),
        atr_period=int(os.environ.get("GRID_ATR_PERIOD", "14")),
        adx_period=int(os.environ.get("ADX_PERIOD", "14")),
        adx_range_threshold=float(os.environ.get("GRID_ADX_RANGE_THRESHOLD", "20")),
        adx_trend_threshold=float(os.environ.get("GRID_ADX_TREND_THRESHOLD", "25")),
        kill_switch_pct=float(os.environ.get("GRID_KILL_SWITCH_PCT", "10")),
        poll_interval_seconds=int(os.environ.get("GRID_POLL_SECONDS", "2")),
        bars_to_fetch=int(os.environ.get("BARS_TO_FETCH", "100")),
        timeframe=os.environ.get("TIMEFRAME", "M5"),
        recenter_levels=int(os.environ.get("GRID_RECENTER_LEVELS", "3")),
        max_slippage_points=int(os.environ.get("MAX_SLIPPAGE_POINTS", "50")),
    )


# ── Bot state ────────────────────────────────────────────────

class _GridBotState:
    def __init__(self) -> None:
        self.running: bool = False
        self.config: Optional[GridConfig] = None
        self.client: Optional[MT5Client] = None
        self.grid: GridState = GridState()
        self.error: Optional[str] = None


_state = _GridBotState()


# ── Weekend check ────────────────────────────────────────────

def _is_weekend(now_utc: datetime) -> bool:
    if now_utc.weekday() == 4 and now_utc.hour >= 21:
        return True
    if now_utc.weekday() == 5:
        return True
    if now_utc.weekday() == 6 and now_utc.hour < 21:
        return True
    return False


# ── Grid Bot Loop ────────────────────────────────────────────

async def _grid_bot_loop() -> None:
    _state.running = True
    log.info("Grid Bot starting.")

    try:
        cfg = _load_grid_config()
        _state.config = cfg

        _state.client = MT5Client(
            login=int(os.environ["MT5_LOGIN"]),
            password=os.environ["MT5_PASSWORD"],
            server=os.environ["MT5_SERVER"],
        )
        _state.client.connect()

        # ── Safety gate ──────────────────────────────────────
        if not _state.client.is_demo():
            live_ok = os.environ.get("I_UNDERSTAND_THIS_IS_LIVE", "false").lower() == "true"
            if not live_ok:
                msg = (
                    "LIVE account detected. Set I_UNDERSTAND_THIS_IS_LIVE=true "
                    "in .env ONLY after completing demo validation."
                )
                log.critical(msg)
                _state.error = msg
                _state.running = False
                return

        acct = _state.client.account_info()
        _state.grid.session_start_equity = acct["equity"]
        log.info(
            "Account: login=%s  server=%s  balance=%.2f  equity=%.2f  type=%s",
            acct["login"], acct["server"], acct["balance"], acct["equity"],
            "DEMO" if _state.client.is_demo() else "LIVE",
        )

        while _state.running:
            try:
                await asyncio.to_thread(_grid_tick, cfg)
                if _state.error and "MT5 Error" in _state.error:
                    _state.error = None
            except MT5Error as exc:
                log.error("MT5 error: %s. Sleeping 30s and reconnecting...", exc)
                _state.error = f"MT5 Error: {exc} (Reconnecting...)"
                await asyncio.sleep(30)
                try:
                    if _state.running:
                        await asyncio.to_thread(_state.client.connect)
                        log.info("Reconnected to MT5 successfully.")
                except Exception as reconnect_exc:
                    log.error("Reconnect failed: %s", reconnect_exc)
            except Exception as exc:  # noqa: BLE001
                log.exception("Unexpected error in grid tick: %s", exc)
                _state.error = str(exc)

            await asyncio.sleep(cfg.poll_interval_seconds)

    except Exception as exc:  # noqa: BLE001
        log.exception("Fatal grid bot error: %s", exc)
        _state.error = str(exc)
        _state.running = False
    finally:
        if _state.client:
            _state.client.disconnect()


# ── Main tick function ───────────────────────────────────────

def _grid_tick(cfg: GridConfig) -> None:
    """
    One iteration of the grid bot loop.
    Runs synchronously in a thread via asyncio.to_thread().
    """
    now_utc = datetime.utcnow()

    # Weekend: close everything
    if _is_weekend(now_utc):
        if _state.grid.grid_active:
            log.warning("Weekend detected — shutting down grid.")
            _shutdown_grid(cfg)
        return

    # Kill switch check
    if _state.grid.kill_switch_triggered:
        return  # Stay dead until manually restarted

    try:
        equity = _state.client.account_info().get("equity", 0)
    except MT5Error:
        return

    if _state.grid.check_kill_switch(equity, cfg.kill_switch_pct):
        log.critical("⛔ Kill switch activated — closing all grid positions!")
        _emergency_close_all(cfg)
        return

    # Get current price
    tick = _state.client.symbol_info_tick(cfg.symbol)
    bid, ask = tick["bid"], tick["ask"]
    spread = ask - bid

    # Check spread — don't trade if spread is unreasonable
    max_acceptable_spread = cfg.tp_pips * cfg.pip_size * 0.5  # Half of TP
    if spread > max_acceptable_spread:
        log.warning(
            "Spread too wide: %.5f > %.5f (50%% of TP). Waiting...",
            spread, max_acceptable_spread,
        )
        return

    # Compute regime
    rates = _state.client.get_rates(cfg.symbol, cfg.timeframe, cfg.bars_to_fetch)
    indicators = compute_indicators(rates, cfg.atr_period, cfg.adx_period)
    regime = detect_regime(indicators["adx"], cfg.adx_range_threshold, cfg.adx_trend_threshold)

    # Regime change handling
    if regime != _state.grid.regime:
        old_regime = _state.grid.regime
        _state.grid.regime = regime
        _state.grid.last_regime_change = now_utc
        log.info(
            "Regime change: %s → %s (ADX=%.1f)",
            old_regime.value, regime.value, indicators["adx"],
        )

        if regime == MarketRegime.TRENDING:
            log.info("TRENDING detected — cancelling all pending grid orders.")
            _cancel_all_pending_in_mt5(cfg)
        elif old_regime == MarketRegime.TRENDING:
            log.info("Trend ended — rebuilding grid.")
            _rebuild_grid(cfg, bid, ask, indicators["atr"], regime)

    # If grid is not active and regime allows it, build grid
    if not _state.grid.grid_active and regime != MarketRegime.TRENDING:
        _rebuild_grid(cfg, bid, ask, indicators["atr"], regime)

    # Sync state: check for filled orders
    _sync_filled_orders(cfg)

    # Sync state: check for closed positions (TP hit)
    _sync_closed_positions(cfg)

    # Check if grid needs recentering
    if _state.grid.grid_active and _state.grid.spacing > 0:
        mid_price = (bid + ask) / 2
        if should_recenter_grid(mid_price, _state.grid.center_price, _state.grid.spacing, cfg.recenter_levels):
            log.info("Recentering grid around %.2f", mid_price)
            _rebuild_grid(cfg, bid, ask, indicators["atr"], regime)

    # Log periodic status
    _log_status(cfg, bid, ask, indicators)


# ── Grid management functions ────────────────────────────────

def _rebuild_grid(cfg: GridConfig, bid: float, ask: float, atr: float, regime: MarketRegime) -> None:
    """Cancel existing pending orders and build a fresh grid."""
    # Cancel all existing pending orders in MT5
    _cancel_all_pending_in_mt5(cfg)

    # Clean up state
    _state.grid.remove_closed_and_cancelled()

    # Calculate spacing
    spacing = calculate_grid_spacing(
        atr_value=atr,
        pip_size=cfg.pip_size,
        multiplier=cfg.atr_multiplier,
        min_spacing_pips=cfg.min_spacing_pips,
        max_spacing_pips=cfg.max_spacing_pips,
    )

    # Adjust levels for regime
    above, below = levels_for_regime(regime, cfg.levels_above, cfg.levels_below)

    if above == 0 and below == 0:
        _state.grid.grid_active = False
        return

    center = round((bid + ask) / 2, 2)

    # Generate new grid levels
    levels = generate_grid(
        center_price=center,
        spacing=spacing,
        levels_above=above,
        levels_below=below,
        tp_pips=cfg.tp_pips,
        pip_size=cfg.pip_size,
        lot_size=cfg.lot_size,
    )

    # Place limit orders in MT5
    placed_levels = []
    for lv in levels:
        try:
            result = _state.client.place_limit_order(
                symbol=cfg.symbol,
                direction=lv.direction,
                lot=lv.lot_size,
                price=lv.order_price,
                tp=lv.tp_price,
            )
            lv.order_ticket = result.get("order")
            placed_levels.append(lv)
        except MT5Error as exc:
            log.warning(
                "Failed to place %s limit @ %.2f: %s",
                lv.direction, lv.order_price, exc,
            )

    # Update state
    # Keep filled levels (they have open positions), replace pending
    existing_filled = _state.grid.filled_levels
    _state.grid.levels = existing_filled + placed_levels
    _state.grid.center_price = center
    _state.grid.spacing = spacing
    _state.grid.grid_active = True

    log.info(
        "Grid built: center=%.2f spacing=%.5f placed=%d/%d (regime=%s)",
        center, spacing, len(placed_levels), len(levels), regime.value,
    )


def _cancel_all_pending_in_mt5(cfg: GridConfig) -> None:
    """Cancel all pending grid orders in MT5 and update state."""
    try:
        _state.client.cancel_all_pending(cfg.symbol, magic=990100)
    except MT5Error as exc:
        log.warning("Error cancelling pending orders: %s", exc)

    # Mark all pending levels as cancelled in state
    cancelled = _state.grid.cancel_all_pending()
    if cancelled:
        log.info("Cancelled %d pending grid levels in state.", len(cancelled))


def _sync_filled_orders(cfg: GridConfig) -> None:
    """
    Check if any pending grid orders have been filled.
    When MT5 fills a limit order, it disappears from pending orders
    and appears as an open position.
    """
    positions = _state.client.get_positions_by_magic(cfg.symbol, magic=990100)
    position_tickets = {p["ticket"] for p in positions}

    # Check each pending level: if its order_ticket no longer exists in pending
    # orders but a position exists, it was filled
    pending_orders = _state.client.get_pending_orders(cfg.symbol, magic=990100)
    pending_tickets = {o["ticket"] for o in pending_orders}

    for lv in list(_state.grid.pending_levels):
        if lv.order_ticket is None:
            continue

        # If the order ticket is no longer pending, check for a matching position
        if lv.order_ticket not in pending_tickets:
            # Look for a position that might have been created from this order
            # MT5 positions have a different ticket than the order that created them.
            # We need to check deal history to match order → position
            matched_pos = _find_position_for_order(lv.order_ticket)
            if matched_pos is not None:
                _state.grid.mark_filled(
                    order_ticket=lv.order_ticket,
                    position_ticket=matched_pos["ticket"],
                    fill_price=matched_pos["price_open"],
                    fill_time=datetime.utcfromtimestamp(matched_pos["time"]),
                )
            else:
                # Order disappeared but no position found — cancelled externally
                lv.status = GridLevelStatus.CANCELLED
                log.warning(
                    "Pending order %d disappeared without a position. "
                    "May have been cancelled externally.",
                    lv.order_ticket,
                )


def _find_position_for_order(order_ticket: int) -> Optional[dict]:
    """
    Try to find the MT5 position that was opened by a specific order.
    Checks deal history to trace order_ticket → position.
    """
    try:
        # Get all deals that match this order
        deals = None
        try:
            import MetaTrader5 as mt5
            deals = mt5.history_deals_get(order=order_ticket)
        except Exception:
            pass

        if deals:
            for deal in deals:
                d = deal._asdict() if hasattr(deal, "_asdict") else deal
                if d.get("entry") == 0:  # DEAL_ENTRY_IN
                    pos_id = d.get("position_id")
                    if pos_id:
                        # Now find the position by this ID
                        positions = mt5.positions_get(ticket=pos_id)
                        if positions:
                            return positions[0]._asdict()
    except Exception as exc:
        log.debug("Could not trace order %d to position: %s", order_ticket, exc)

    # Fallback: check all grid positions for one near the expected price
    return None


def _sync_closed_positions(cfg: GridConfig) -> None:
    """
    Check if any filled grid positions have been closed (TP hit).
    When MT5 closes a position (TP hit), it disappears from open positions.
    """
    positions = _state.client.get_positions_by_magic(cfg.symbol, magic=990100)
    open_tickets = {p["ticket"] for p in positions}

    for lv in list(_state.grid.filled_levels):
        if lv.position_ticket is None:
            continue

        if lv.position_ticket not in open_tickets:
            # Position closed — likely TP hit
            close_deal = None
            try:
                close_deal = _state.client.get_close_deal(lv.position_ticket)
            except MT5Error:
                pass

            if close_deal:
                close_price = close_deal["price"]
                close_time = datetime.utcfromtimestamp(close_deal["time"])
            else:
                close_price = lv.tp_price  # Assume TP hit
                close_time = datetime.utcnow()

            _state.grid.mark_closed(
                position_ticket=lv.position_ticket,
                close_price=close_price,
                pip_size=cfg.pip_size,
                close_time=close_time,
            )

            # Log to database
            equity = None
            try:
                equity = _state.client.account_info().get("equity")
            except MT5Error:
                pass

            record = GridTradeRecord(
                direction=lv.direction,
                entry_time=lv.fill_time or datetime.utcnow(),
                exit_time=close_time,
                entry_price=lv.fill_price or lv.order_price,
                exit_price=close_price,
                tp_price=lv.tp_price,
                pips=lv.pips,
                lot_size=lv.lot_size,
                running_equity=equity,
            )
            trade_id = trade_log.log_grid_trade(record)
            log.info(
                "Grid trade logged [db_id=%d]: %s %+.1f pips  equity=%s",
                trade_id, lv.direction, lv.pips or 0, f"{equity:.2f}" if equity else "n/a",
            )

            # Replenish: place a new limit order at the same level
            _replenish_level(cfg, lv)


def _replenish_level(cfg: GridConfig, closed_level: GridLevel) -> None:
    """
    After a grid level's TP is hit, place a fresh limit order at the same price
    to keep the grid full.
    """
    # Only replenish if grid is still active and regime allows it
    if not _state.grid.grid_active:
        return
    if _state.grid.regime == MarketRegime.TRENDING:
        return

    try:
        result = _state.client.place_limit_order(
            symbol=cfg.symbol,
            direction=closed_level.direction,
            lot=closed_level.lot_size,
            price=closed_level.order_price,
            tp=closed_level.tp_price,
        )
        new_level = GridLevel(
            direction=closed_level.direction,
            order_price=closed_level.order_price,
            tp_price=closed_level.tp_price,
            lot_size=closed_level.lot_size,
            status=GridLevelStatus.PENDING,
            order_ticket=result.get("order"),
        )
        _state.grid.levels.append(new_level)
        log.info(
            "Replenished grid level: %s @ %.2f (ticket=%s)",
            new_level.direction, new_level.order_price, new_level.order_ticket,
        )
    except MT5Error as exc:
        log.warning(
            "Failed to replenish %s level @ %.2f: %s",
            closed_level.direction, closed_level.order_price, exc,
        )


def _shutdown_grid(cfg: GridConfig) -> None:
    """Cancel all pending orders and close all filled positions."""
    _cancel_all_pending_in_mt5(cfg)

    # Close all filled positions
    for lv in list(_state.grid.filled_levels):
        if lv.position_ticket:
            try:
                _state.client.close_position_market(
                    lv.position_ticket, cfg.symbol, cfg.max_slippage_points
                )
                log.info("Closed grid position: ticket=%d", lv.position_ticket)
            except MT5Error as exc:
                log.error("Failed to close position %d: %s", lv.position_ticket, exc)

    _state.grid.grid_active = False


def _emergency_close_all(cfg: GridConfig) -> None:
    """Kill-switch: close everything immediately."""
    log.critical("⛔ EMERGENCY CLOSE — shutting down entire grid!")
    _shutdown_grid(cfg)
    _state.grid.kill_switch_triggered = True
    _state.error = "Kill switch triggered — grid bot stopped. Restart manually after review."


def _log_status(cfg: GridConfig, bid: float, ask: float, indicators: dict) -> None:
    """Log periodic status summary."""
    unrealized = _state.grid.compute_unrealized_pnl(bid, ask, cfg.pip_size)
    log.info(
        "Grid status: regime=%s ADX=%.1f ATR=%.5f | pending=%d filled=%d | "
        "realized=%+.1f pips (%d trades) unrealized=%+.1f pips | price=%.2f",
        _state.grid.regime.value, indicators["adx"], indicators["atr"],
        _state.grid.pending_count, _state.grid.filled_count,
        _state.grid.total_realized_pips, _state.grid.total_realized_trades,
        unrealized, (bid + ask) / 2,
    )


# ── FastAPI app ───────────────────────────────────────────────

@asynccontextmanager
async def _lifespan(app: FastAPI):
    trade_log.init_db()
    task = asyncio.create_task(_grid_bot_loop())
    yield
    _state.running = False
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(title="K9 Grid Bot — XAUUSD Smart Grid", lifespan=_lifespan)


@app.get("/grid/status", response_model=GridStatus, summary="Grid bot health and status")
async def grid_status() -> GridStatus:
    equity = balance = account_type = None
    if _state.client and _state.running:
        try:
            info = _state.client.account_info()
            equity = info.get("equity")
            balance = info.get("balance")
            account_type = "demo" if _state.client.is_demo() else "real"
        except MT5Error:
            pass

    summary = _state.grid.summary()
    return GridStatus(
        running=_state.running,
        grid_active=summary["grid_active"],
        regime=summary["regime"],
        center_price=summary["center_price"],
        spacing=summary["spacing"],
        pending_orders=summary["pending_orders"],
        open_positions=summary["open_positions"],
        total_realized_pips=summary["total_realized_pips"],
        total_realized_trades=summary["total_realized_trades"],
        win_rate=summary["win_rate"],
        kill_switch_triggered=summary["kill_switch_triggered"],
        account_equity=equity,
        account_balance=balance,
        account_type=account_type,
        error=_state.error,
    )


@app.get("/grid/trades", summary="All grid trades from database")
async def grid_trades() -> list:
    return trade_log.get_grid_trades()


@app.get("/grid/summary", summary="Grid trading performance summary")
async def grid_summary() -> dict:
    return trade_log.get_grid_summary()


@app.get("/grid/config", response_model=GridConfig, summary="Current grid configuration")
async def grid_config() -> GridConfig:
    return _state.config or _load_grid_config()


@app.post("/grid/stop", summary="Stop the grid bot gracefully")
async def grid_stop() -> dict:
    if not _state.running:
        raise HTTPException(400, "Grid bot is not running.")
    cfg = _state.config or _load_grid_config()
    await asyncio.to_thread(_shutdown_grid, cfg)
    return {"status": "Grid bot stopped. All pending orders cancelled, positions closed."}


@app.post("/grid/reset-killswitch", summary="Reset the kill switch after manual review")
async def reset_kill_switch() -> dict:
    if not _state.grid.kill_switch_triggered:
        return {"status": "Kill switch was not triggered."}
    _state.grid.kill_switch_triggered = False
    _state.error = None
    # Update session equity to current
    try:
        acct = _state.client.account_info()
        _state.grid.session_start_equity = acct["equity"]
    except MT5Error:
        pass
    return {"status": "Kill switch reset. Grid bot will resume on next tick."}

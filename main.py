"""
K9 Gold Bot — FastAPI service
Runs the XAUUSD trend-capture bot and exposes a status/control API.
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
import tuning as tuning_module
from models import BotConfig, BotStatus, TradeRecord, TuningSuggestion
from mt5_client import MT5Client, MT5Error
from strategy import (
    Direction,
    PositionState,
    calc_pips,
    classify_exit,
    compute_signals,
    update_trailing_stop,
)

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("k9bot.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("k9bot")


# ── Config ───────────────────────────────────────────────────

def _load_config() -> BotConfig:
    return BotConfig(
        symbol=os.environ["SYMBOL"],
        pip_size=float(os.environ.get("PIP_SIZE", "0.01")),
        adx_threshold=float(os.environ.get("ADX_THRESHOLD", "20")),
        adx_threshold_min=float(os.environ.get("ADX_THRESHOLD_MIN", "15")),
        adx_threshold_max=float(os.environ.get("ADX_THRESHOLD_MAX", "35")),
        initial_stop_pips=float(os.environ.get("INITIAL_STOP_PIPS", "7")),
        trail_pips=float(os.environ.get("TRAIL_PIPS", "5")),
        lot_size=float(os.environ.get("LOT_SIZE", "0.01")),
        ema_fast=int(os.environ.get("EMA_FAST", "9")),
        ema_slow=int(os.environ.get("EMA_SLOW", "21")),
        adx_period=int(os.environ.get("ADX_PERIOD", "14")),
        timeframe=os.environ.get("TIMEFRAME", "M5"),
        poll_interval_seconds=int(os.environ.get("POLL_INTERVAL_SECONDS", "5")),
        bars_to_fetch=int(os.environ.get("BARS_TO_FETCH", "100")),
        tuning_review_every_n_trades=int(os.environ.get("TUNING_REVIEW_EVERY_N_TRADES", "20")),
        adx_tune_step=float(os.environ.get("ADX_TUNE_STEP", "1.0")),
        auto_apply_tuning=os.environ.get("AUTO_APPLY_TUNING", "false").lower() == "true",
    )


# ── Bot state ────────────────────────────────────────────────

class _BotState:
    def __init__(self) -> None:
        self.running: bool = False
        self.config: Optional[BotConfig] = None
        self.client: Optional[MT5Client] = None
        self.position: Optional[PositionState] = None
        self.mt5_ticket: Optional[int] = None
        self.error: Optional[str] = None
        self.closed_trades_count: int = 0
        self.last_tuning_check_count: int = 0
        # Live ADX threshold — may be updated by tuning approval without restart
        self.adx_threshold: float = 20.0

    @property
    def in_position(self) -> bool:
        return self.position is not None


_state = _BotState()


# ── Bot loop ─────────────────────────────────────────────────

async def _bot_loop() -> None:
    _state.running = True
    log.info("Bot starting.")

    try:
        cfg = _load_config()
        _state.config = cfg
        _state.adx_threshold = cfg.adx_threshold

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
                    "LIVE account detected. This bot is for DEMO use during the validation "
                    "phase (200+ trades / 4 weeks minimum). Set I_UNDERSTAND_THIS_IS_LIVE=true "
                    "in .env ONLY after completing demo validation."
                )
                log.critical(msg)
                _state.error = msg
                _state.running = False
                return

        acct = _state.client.account_info()
        log.info(
            "Account: login=%s  server=%s  balance=%.2f  type=%s",
            acct["login"], acct["server"], acct["balance"],
            "DEMO" if _state.client.is_demo() else "LIVE",
        )

        # Reconcile: pick up any position left open from a previous run
        _reconcile_existing_position(cfg)

        _state.closed_trades_count = len(trade_log.get_closed_trades())
        _state.last_tuning_check_count = _state.closed_trades_count

        while _state.running:
            try:
                await _tick(cfg)
            except MT5Error as exc:
                log.error("MT5 error: %s", exc)
                _state.error = str(exc)
            except Exception as exc:  # noqa: BLE001
                log.exception("Unexpected error in tick: %s", exc)
                _state.error = str(exc)

            await asyncio.sleep(cfg.poll_interval_seconds)

    except Exception as exc:  # noqa: BLE001
        log.exception("Fatal bot error: %s", exc)
        _state.error = str(exc)
        _state.running = False
    finally:
        if _state.client:
            _state.client.disconnect()


def _reconcile_existing_position(cfg: BotConfig) -> None:
    """If MT5 already has an open position (e.g., bot restarted mid-trade), track it."""
    positions = _state.client.get_positions(cfg.symbol)
    if not positions:
        return
    pos = positions[0]
    ticket = pos["ticket"]
    direction = Direction.BUY if pos["type"] == 0 else Direction.SELL
    sl = pos["sl"] or (pos["price_open"] - cfg.initial_stop_pips * cfg.pip_size
                       if direction == Direction.BUY
                       else pos["price_open"] + cfg.initial_stop_pips * cfg.pip_size)
    _state.position = PositionState(
        direction=direction,
        entry_price=pos["price_open"],
        initial_stop=sl,
        stop_level=sl,
        adx_at_entry=0.0,
        entry_time=datetime.utcfromtimestamp(pos["time"]),
    )
    _state.mt5_ticket = ticket
    log.warning(
        "Reconciled existing MT5 position: ticket=%d %s @ %.5f  SL=%.5f",
        ticket, direction.value, pos["price_open"], sl,
    )


async def _tick(cfg: BotConfig) -> None:
    tick = _state.client.symbol_info_tick(cfg.symbol)
    bid, ask = tick["bid"], tick["ask"]

    if _state.in_position:
        await _manage_position(cfg, bid, ask)
    else:
        await _scan_for_entry(cfg, bid, ask)

    _maybe_run_tuning(cfg)


async def _scan_for_entry(cfg: BotConfig, bid: float, ask: float) -> None:
    rates = _state.client.get_rates(cfg.symbol, cfg.timeframe, cfg.bars_to_fetch)
    sig = compute_signals(rates, cfg.ema_fast, cfg.ema_slow, cfg.adx_period)

    if sig["adx"] < _state.adx_threshold:
        return

    direction: Optional[Direction] = None
    if sig["bull_cross"]:
        direction = Direction.BUY
    elif sig["bear_cross"]:
        direction = Direction.SELL

    if direction is None:
        return

    stop_dist = cfg.initial_stop_pips * cfg.pip_size
    if direction == Direction.BUY:
        entry_approx = ask
        sl = round(entry_approx - stop_dist, 2)
    else:
        entry_approx = bid
        sl = round(entry_approx + stop_dist, 2)

    log.info(
        "Signal: %s  ADX=%.1f  close=%.5f  proposed_sl=%.5f",
        direction.value, sig["adx"], sig["close"], sl,
    )

    result = _state.client.place_market_order(
        symbol=cfg.symbol,
        direction=direction.value,
        lot=cfg.lot_size,
        sl=sl,
    )

    actual_entry = result.get("price", entry_approx)
    ticket = result.get("order")

    _state.mt5_ticket = ticket
    _state.position = PositionState(
        direction=direction,
        entry_price=actual_entry,
        initial_stop=sl,
        stop_level=sl,
        adx_at_entry=sig["adx"],
        entry_time=datetime.utcnow(),
    )
    log.info("Position open — ticket=%s  entry=%.5f  SL=%.5f", ticket, actual_entry, sl)


async def _manage_position(cfg: BotConfig, bid: float, ask: float) -> None:
    # Check whether MT5 still holds the position
    positions = _state.client.get_positions(cfg.symbol)
    mt5_pos = next((p for p in positions if p["ticket"] == _state.mt5_ticket), None)

    if mt5_pos is None:
        # MT5 closed it (stop hit server-side, or manual intervention)
        _close_and_log(cfg)
        return

    old_stop = _state.position.stop_level
    _state.position, _ = update_trailing_stop(
        _state.position, bid, ask, cfg.trail_pips, cfg.pip_size
    )

    if _state.position.stop_level != old_stop:
        new_sl = round(_state.position.stop_level, 2)
        try:
            _state.client.modify_position_sl(_state.mt5_ticket, cfg.symbol, new_sl)
            log.debug("Trailing SL → %.5f", new_sl)
        except MT5Error as exc:
            log.warning("Could not update SL in MT5: %s", exc)


def _close_and_log(cfg: BotConfig) -> None:
    pos = _state.position
    reason = classify_exit(pos)

    # Try to get actual close price from MT5 deal history
    close_deal = None
    if _state.mt5_ticket is not None:
        try:
            close_deal = _state.client.get_close_deal(_state.mt5_ticket)
        except MT5Error:
            pass

    if close_deal:
        exit_price = close_deal["price"]
        exit_time = datetime.utcfromtimestamp(close_deal["time"])
    else:
        exit_price = pos.stop_level
        exit_time = datetime.utcnow()
        log.warning("Close deal not found in history — using local stop_level as exit price.")

    pips = calc_pips(pos, exit_price, cfg.pip_size)

    equity: Optional[float] = None
    try:
        equity = _state.client.account_info().get("equity")
    except MT5Error:
        pass

    record = TradeRecord(
        direction=pos.direction.value,
        entry_time=pos.entry_time,
        exit_time=exit_time,
        entry_price=pos.entry_price,
        exit_price=exit_price,
        exit_reason=reason,
        pips=round(pips, 2),
        adx_at_entry=pos.adx_at_entry,
        lot_size=cfg.lot_size,
        running_equity=equity,
    )
    trade_id = trade_log.log_trade(record)
    _state.closed_trades_count += 1

    log.info(
        "Trade closed [db_id=%d] ticket=%s  %s  %+.1f pips  reason=%s  equity=%s",
        trade_id, _state.mt5_ticket, pos.direction.value,
        pips, reason, f"{equity:.2f}" if equity else "n/a",
    )

    _state.position = None
    _state.mt5_ticket = None


def _maybe_run_tuning(cfg: BotConfig) -> None:
    new_count = len(trade_log.get_closed_trades())
    since_last = new_count - _state.last_tuning_check_count
    if since_last < cfg.tuning_review_every_n_trades:
        return

    _state.last_tuning_check_count = new_count
    suggestion = tuning_module.analyze_and_suggest(
        current_threshold=_state.adx_threshold,
        threshold_min=cfg.adx_threshold_min,
        threshold_max=cfg.adx_threshold_max,
        tune_step=cfg.adx_tune_step,
        n_trades=cfg.tuning_review_every_n_trades,
    )
    if suggestion is None:
        return

    sid = trade_log.save_tuning_suggestion(suggestion)
    log.info("Tuning suggestion saved [id=%d].", sid)

    if cfg.auto_apply_tuning:
        trade_log.apply_tuning(sid)
        _state.adx_threshold = suggestion.suggested_threshold
        log.warning(
            "AUTO_APPLY_TUNING=true: ADX threshold updated to %.1f in memory. "
            "Update ADX_THRESHOLD in .env to persist across restarts.",
            _state.adx_threshold,
        )


# ── FastAPI app ───────────────────────────────────────────────

@asynccontextmanager
async def _lifespan(app: FastAPI):
    trade_log.init_db()
    task = asyncio.create_task(_bot_loop())
    yield
    _state.running = False
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(title="K9 Gold Bot — XAUUSD Trend Capture", lifespan=_lifespan)


@app.get("/status", response_model=BotStatus, summary="Bot health and current position")
async def status() -> BotStatus:
    equity = balance = account_type = None
    if _state.client and _state.running:
        try:
            info = _state.client.account_info()
            equity = info.get("equity")
            balance = info.get("balance")
            account_type = "demo" if _state.client.is_demo() else "real"
        except MT5Error:
            pass

    pos = _state.position
    return BotStatus(
        running=_state.running,
        in_position=_state.in_position,
        current_direction=pos.direction.value if pos else None,
        current_entry_price=pos.entry_price if pos else None,
        current_stop=pos.stop_level if pos else None,
        current_adx_threshold=_state.adx_threshold,
        total_closed_trades=_state.closed_trades_count,
        account_equity=equity,
        account_balance=balance,
        account_type=account_type,
        error=_state.error,
    )


@app.get("/trades", summary="All logged trades (open + closed)")
async def trades() -> list:
    return trade_log.get_all_trades()


@app.get("/config", response_model=BotConfig, summary="Current bot configuration")
async def config() -> BotConfig:
    return _state.config or _load_config()


@app.get("/tuning/pending", summary="Suggested but unapplied ADX threshold changes")
async def tuning_pending() -> list:
    return trade_log.get_pending_tuning()


@app.post("/tuning/approve/{suggestion_id}", summary="Apply a pending tuning suggestion")
async def tuning_approve(suggestion_id: int) -> dict:
    pending = trade_log.get_pending_tuning()
    match = next((s for s in pending if s.id == suggestion_id), None)
    if match is None:
        raise HTTPException(404, f"No pending suggestion with id={suggestion_id}")

    trade_log.apply_tuning(suggestion_id)
    _state.adx_threshold = match.suggested_threshold

    log.info(
        "Human approved tuning [id=%d]: ADX threshold %.1f → %.1f",
        suggestion_id, match.current_threshold, match.suggested_threshold,
    )
    return {
        "approved_id": suggestion_id,
        "new_threshold_live": match.suggested_threshold,
        "note": "Threshold updated in memory. Update ADX_THRESHOLD in .env to persist across restarts.",
    }

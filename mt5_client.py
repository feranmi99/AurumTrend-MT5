"""
Wraps the MetaTrader5 Python API.  All broker I/O lives here.

This package is Windows-only (wraps MT5 terminal IPC).  On non-Windows
systems (or when the package is absent) the import guard will raise MT5Error
before any network call is made, so other modules can still be imported and
tested without a live MT5 terminal.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

log = logging.getLogger(__name__)

try:
    import MetaTrader5 as mt5  # type: ignore
    _MT5_AVAILABLE = True
except ImportError:
    _MT5_AVAILABLE = False


class MT5Error(Exception):
    pass


def _require_mt5() -> None:
    if not _MT5_AVAILABLE:
        raise MT5Error(
            "MetaTrader5 package is not installed. "
            "Run 'pip install MetaTrader5' on a Windows machine with MT5 desktop installed."
        )


class MT5Client:
    def __init__(self, login: int, password: str, server: str) -> None:
        _require_mt5()
        self._login = login
        self._password = password
        self._server = server
        self._connected = False

    # ── Connection ───────────────────────────────────────────

    def connect(self) -> None:
        if not mt5.initialize(login=self._login, password=self._password, server=self._server):
            raise MT5Error(f"mt5.initialize failed: {mt5.last_error()}")
        self._connected = True
        info = mt5.terminal_info()
        log.info("MT5 connected — build %s, connected=%s", info.build, info.connected)

    def disconnect(self) -> None:
        if self._connected:
            mt5.shutdown()
            self._connected = False
            log.info("MT5 disconnected.")

    def _check(self) -> None:
        if not self._connected:
            raise MT5Error("Not connected to MT5. Call connect() first.")

    # ── Account ──────────────────────────────────────────────

    def account_info(self) -> dict:
        self._check()
        info = mt5.account_info()
        if info is None:
            raise MT5Error(f"mt5.account_info() failed: {mt5.last_error()}")
        return info._asdict()

    def is_demo(self) -> bool:
        return self.account_info().get("trade_mode") == mt5.ACCOUNT_TRADE_MODE_DEMO

    # ── Market data ──────────────────────────────────────────

    def symbol_info_tick(self, symbol: str) -> dict:
        self._check()
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            raise MT5Error(f"symbol_info_tick({symbol!r}) failed: {mt5.last_error()}")
        return tick._asdict()

    def symbol_info(self, symbol: str) -> dict:
        self._check()
        info = mt5.symbol_info(symbol)
        if info is None:
            raise MT5Error(f"symbol_info({symbol!r}) failed: {mt5.last_error()}")
        if not info.visible:
            mt5.symbol_select(symbol, True)
        return info._asdict()

    def get_rates(self, symbol: str, timeframe: str, count: int) -> list:
        """
        Return the last `count` OHLCV bars as a list of dicts.
        timeframe: 'M1' | 'M5' | 'M15' | 'H1'
        """
        self._check()
        tf_map = {
            "M1": mt5.TIMEFRAME_M1,
            "M5": mt5.TIMEFRAME_M5,
            "M15": mt5.TIMEFRAME_M15,
            "H1": mt5.TIMEFRAME_H1,
        }
        tf = tf_map.get(timeframe, mt5.TIMEFRAME_M5)
        rates = mt5.copy_rates_from_pos(symbol, tf, 0, count)
        if rates is None or len(rates) == 0:
            raise MT5Error(f"copy_rates_from_pos failed: {mt5.last_error()}")
        return [dict(r) for r in rates]

    # ── Positions ────────────────────────────────────────────

    def get_positions(self, symbol: str) -> list:
        self._check()
        positions = mt5.positions_get(symbol=symbol)
        return [p._asdict() for p in positions] if positions else []

    # ── Orders ───────────────────────────────────────────────

    def place_market_order(
        self,
        symbol: str,
        direction: str,
        lot: float,
        sl: float,
        comment: str = "k9bot",
    ) -> dict:
        self._check()
        tick = self.symbol_info_tick(symbol)
        sym = self.symbol_info(symbol)

        order_type = mt5.ORDER_TYPE_BUY if direction == "buy" else mt5.ORDER_TYPE_SELL
        price = tick["ask"] if direction == "buy" else tick["bid"]

        # Prefer the symbol's native filling mode; fall back to IOC
        filling_modes = sym.get("filling_mode", mt5.ORDER_FILLING_IOC)
        if filling_modes & mt5.ORDER_FILLING_FOK:
            filling = mt5.ORDER_FILLING_FOK
        elif filling_modes & mt5.ORDER_FILLING_IOC:
            filling = mt5.ORDER_FILLING_IOC
        else:
            filling = mt5.ORDER_FILLING_RETURN

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": float(lot),
            "type": order_type,
            "price": price,
            "sl": float(sl),
            "tp": 0.0,
            "deviation": 20,
            "magic": 990099,
            "comment": comment,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": filling,
        }

        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            code = result.retcode if result else "None"
            msg = result.comment if result else ""
            raise MT5Error(f"order_send failed — retcode={code}, comment={msg!r}")

        log.info("Order placed: %s %s %.2f lots @ %.5f  SL=%.5f  ticket=%d",
                 direction, symbol, lot, price, sl, result.order)
        return result._asdict()

    def modify_position_sl(self, position_ticket: int, symbol: str, new_sl: float) -> None:
        self._check()
        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "position": position_ticket,
            "symbol": symbol,
            "sl": float(new_sl),
            "tp": 0.0,
        }
        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            code = result.retcode if result else "None"
            raise MT5Error(f"modify_position_sl failed — retcode={code}")
        log.debug("SL updated  ticket=%d  new_sl=%.5f", position_ticket, new_sl)

    # ── Trade history ────────────────────────────────────────

    def get_close_deal(self, position_ticket: int, lookback_hours: int = 24) -> Optional[dict]:
        """
        Return the closing deal for a position from recent deal history.
        Returns None if not found (position may still be open or history unavailable).
        """
        self._check()
        now = datetime.utcnow()
        from_date = now - timedelta(hours=lookback_hours)
        deals = mt5.history_deals_get(from_date, now)
        if deals is None:
            return None
        for deal in deals:
            if deal.position_id == position_ticket and deal.entry == mt5.DEAL_ENTRY_OUT:
                return deal._asdict()
        return None

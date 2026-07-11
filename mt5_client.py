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

    @staticmethod
    def _rate_to_dict(rate: object) -> dict:
        if isinstance(rate, dict):
            return rate
        if hasattr(rate, "_asdict"):
            return rate._asdict()
        if hasattr(rate, "tolist"):
            rate = rate.tolist()
        if isinstance(rate, (tuple, list)) and len(rate) >= 5:
            return {
                "time": rate[0],
                "open": rate[1],
                "high": rate[2],
                "low": rate[3],
                "close": rate[4],
                "tick_volume": rate[5] if len(rate) > 5 else None,
                "spread": rate[6] if len(rate) > 6 else None,
                "real_volume": rate[7] if len(rate) > 7 else None,
            }
        raise MT5Error(f"Unsupported MT5 rate format: {type(rate).__name__}")

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
        return [self._rate_to_dict(r) for r in rates]

    # ── Positions ────────────────────────────────────────────

    def get_positions(self, symbol: str) -> list:
        self._check()
        positions = mt5.positions_get(symbol=symbol)
        return [p._asdict() for p in positions] if positions else []

    # ── Orders ───────────────────────────────────────────────

    def place_market_order(
        self, symbol: str, direction: str, lot: float, sl: float, comment: str = "k9bot", deviation: int = 20
    ) -> dict:
        self._check()
        tick = self.symbol_info_tick(symbol)
        sym = self.symbol_info(symbol)

        point = sym["point"]
        digits = sym["digits"]
        # stop_level = sym["trade_stops_level"] * point
        stop_level = max(sym["trade_stops_level"] * point, 50 * point)

        order_type = mt5.ORDER_TYPE_BUY if direction == "buy" else mt5.ORDER_TYPE_SELL

        price = tick["ask"] if direction == "buy" else tick["bid"]

        # Ensure SL respects broker minimum stop distance
        if direction == "buy":
            if tick["bid"] - sl < stop_level:
                sl = tick["bid"] - stop_level
        else:
            if sl - tick["ask"] < stop_level:
                sl = tick["ask"] + stop_level

        # Round to symbol precision
        price = round(price, digits)
        sl = round(sl, digits)

        # order_type = mt5.ORDER_TYPE_BUY if direction == "buy" else mt5.ORDER_TYPE_SELL
        # price = tick["ask"] if direction == "buy" else tick["bid"]

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
            "deviation": deviation,
            "magic": 990099,
            "comment": comment,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": filling,
        }

        log.info(
            "Order Check | Bid=%.5f Ask=%.5f Price=%.5f SL=%.5f StopLevel=%d Point=%f",
            tick["bid"],
            tick["ask"],
            price,
            sl,
            sym["trade_stops_level"],
            point,
        )

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

        # Enforce broker minimum stop distance for modifications
        tick = self.symbol_info_tick(symbol)
        sym = self.symbol_info(symbol)
        point = sym["point"]
        digits = sym["digits"]
        stop_level = max(sym["trade_stops_level"] * point, 50 * point)

        # Get position to check direction
        positions = mt5.positions_get(ticket=position_ticket)
        if positions:
            pos = positions[0]
            if pos.type == mt5.POSITION_TYPE_BUY:
                max_sl = tick["bid"] - stop_level
                if new_sl > max_sl:
                    new_sl = max_sl
            elif pos.type == mt5.POSITION_TYPE_SELL:
                min_sl = tick["ask"] + stop_level
                if new_sl < min_sl:
                    new_sl = min_sl

        new_sl = round(new_sl, digits)

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
            msg = result.comment if result else ""
            raise MT5Error(f"modify_position_sl failed — retcode={code}, comment={msg!r}")
        log.debug("SL updated  ticket=%d  new_sl=%.5f", position_ticket, new_sl)

    def close_position(self, position_ticket: int, symbol: str, deviation: int = 50) -> dict:
        """Closes an open position by sending an opposite market deal."""
        self._check()
        positions = mt5.positions_get(ticket=position_ticket)
        if not positions:
            return {}
        pos = positions[0]
        tick = self.symbol_info_tick(symbol)
        
        order_type = mt5.ORDER_TYPE_SELL if pos.type == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_BUY
        price = tick["bid"] if order_type == mt5.ORDER_TYPE_SELL else tick["ask"]
        
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "position": position_ticket,
            "symbol": symbol,
            "volume": pos.volume,
            "type": order_type,
            "price": price,
            "deviation": deviation,
            "magic": 990099,
            "comment": "k9bot autoclose",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            code = result.retcode if result else "None"
            msg = result.comment if result else ""
            raise MT5Error(f"close_position failed — retcode={code}, comment={msg!r}")
        return result._asdict()

    def get_close_deal(self, position_ticket: int) -> Optional[dict]:
        """
        Return the closing deal for a position from deal history.
        Returns None if not found (position may still be open or history unavailable).
        """
        self._check()
        # Fetching by position ID avoids all timezone-offset bugs and is much faster
        deals = mt5.history_deals_get(position=position_ticket)
        if deals is None:
            return None
        for deal in deals:
            if deal.entry == mt5.DEAL_ENTRY_OUT:
                return deal._asdict()
        return None

"""SQLite persistence for closed trades and tuning suggestions."""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from typing import List, Optional

from models import GridTradeRecord, TradeRecord, TuningSuggestion

DB_PATH = os.environ.get("DB_PATH", "trades.db")


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init_db() -> None:
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                direction       TEXT    NOT NULL,
                entry_time      TEXT    NOT NULL,
                exit_time       TEXT,
                entry_price     REAL    NOT NULL,
                exit_price      REAL,
                exit_reason     TEXT,
                pips            REAL,
                adx_at_entry    REAL    NOT NULL,
                lot_size        REAL    NOT NULL,
                running_equity  REAL
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS tuning_suggestions (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at          TEXT    NOT NULL,
                current_threshold   REAL    NOT NULL,
                suggested_threshold REAL    NOT NULL,
                reasoning           TEXT    NOT NULL,
                applied             INTEGER NOT NULL DEFAULT 0,
                applied_at          TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS grid_trades (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                direction       TEXT    NOT NULL,
                entry_time      TEXT    NOT NULL,
                exit_time       TEXT,
                entry_price     REAL    NOT NULL,
                exit_price      REAL,
                tp_price        REAL    NOT NULL,
                pips            REAL,
                lot_size        REAL    NOT NULL,
                running_equity  REAL
            )
        """)
        c.commit()


def log_trade(trade: TradeRecord) -> int:
    with _conn() as c:
        cur = c.execute(
            """INSERT INTO trades
               (direction, entry_time, exit_time, entry_price, exit_price,
                exit_reason, pips, adx_at_entry, lot_size, running_equity)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                trade.direction,
                trade.entry_time.isoformat(),
                trade.exit_time.isoformat() if trade.exit_time else None,
                trade.entry_price,
                trade.exit_price,
                trade.exit_reason,
                trade.pips,
                trade.adx_at_entry,
                trade.lot_size,
                trade.running_equity,
            ),
        )
        c.commit()
        return cur.lastrowid


def get_all_trades() -> List[TradeRecord]:
    with _conn() as c:
        rows = c.execute("SELECT * FROM trades ORDER BY entry_time").fetchall()
    return [_trade(r) for r in rows]


def get_closed_trades() -> List[TradeRecord]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM trades WHERE exit_time IS NOT NULL ORDER BY entry_time"
        ).fetchall()
    return [_trade(r) for r in rows]


def save_tuning_suggestion(suggestion: TuningSuggestion) -> int:
    with _conn() as c:
        cur = c.execute(
            """INSERT INTO tuning_suggestions
               (created_at, current_threshold, suggested_threshold, reasoning, applied)
               VALUES (?,?,?,?,0)""",
            (
                suggestion.created_at.isoformat(),
                suggestion.current_threshold,
                suggestion.suggested_threshold,
                suggestion.reasoning,
            ),
        )
        c.commit()
        return cur.lastrowid


def get_pending_tuning() -> List[TuningSuggestion]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM tuning_suggestions WHERE applied=0 ORDER BY created_at DESC"
        ).fetchall()
    return [_suggestion(r) for r in rows]


def apply_tuning(suggestion_id: int) -> None:
    with _conn() as c:
        c.execute(
            "UPDATE tuning_suggestions SET applied=1, applied_at=? WHERE id=?",
            (datetime.utcnow().isoformat(), suggestion_id),
        )
        c.commit()


def _trade(row: sqlite3.Row) -> TradeRecord:
    return TradeRecord(
        id=row["id"],
        direction=row["direction"],
        entry_time=datetime.fromisoformat(row["entry_time"]),
        exit_time=datetime.fromisoformat(row["exit_time"]) if row["exit_time"] else None,
        entry_price=row["entry_price"],
        exit_price=row["exit_price"],
        exit_reason=row["exit_reason"],
        pips=row["pips"],
        adx_at_entry=row["adx_at_entry"],
        lot_size=row["lot_size"],
        running_equity=row["running_equity"],
    )


def _suggestion(row: sqlite3.Row) -> TuningSuggestion:
    return TuningSuggestion(
        id=row["id"],
        created_at=datetime.fromisoformat(row["created_at"]),
        current_threshold=row["current_threshold"],
        suggested_threshold=row["suggested_threshold"],
        reasoning=row["reasoning"],
        applied=bool(row["applied"]),
        applied_at=datetime.fromisoformat(row["applied_at"]) if row["applied_at"] else None,
    )


# ── Grid Trade Logging ───────────────────────────────────────

def log_grid_trade(trade: GridTradeRecord) -> int:
    """Log a completed grid trade to the database."""
    with _conn() as c:
        cur = c.execute(
            """INSERT INTO grid_trades
               (direction, entry_time, exit_time, entry_price, exit_price,
                tp_price, pips, lot_size, running_equity)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                trade.direction,
                trade.entry_time.isoformat(),
                trade.exit_time.isoformat() if trade.exit_time else None,
                trade.entry_price,
                trade.exit_price,
                trade.tp_price,
                trade.pips,
                trade.lot_size,
                trade.running_equity,
            ),
        )
        c.commit()
        return cur.lastrowid


def get_grid_trades() -> List[GridTradeRecord]:
    """Get all grid trades from the database."""
    with _conn() as c:
        rows = c.execute("SELECT * FROM grid_trades ORDER BY entry_time").fetchall()
    return [_grid_trade(r) for r in rows]


def get_grid_summary() -> dict:
    """Get a summary of grid trading performance."""
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM grid_trades WHERE exit_time IS NOT NULL ORDER BY entry_time"
        ).fetchall()

    if not rows:
        return {
            "total_trades": 0,
            "total_pips": 0.0,
            "win_rate": 0.0,
            "avg_pips": 0.0,
            "best_trade": 0.0,
            "worst_trade": 0.0,
        }

    trades = [_grid_trade(r) for r in rows]
    pips_list = [t.pips for t in trades if t.pips is not None]
    wins = sum(1 for p in pips_list if p > 0)

    return {
        "total_trades": len(trades),
        "total_pips": round(sum(pips_list), 2),
        "win_rate": round(100 * wins / len(pips_list), 1) if pips_list else 0.0,
        "avg_pips": round(sum(pips_list) / len(pips_list), 2) if pips_list else 0.0,
        "best_trade": round(max(pips_list), 2) if pips_list else 0.0,
        "worst_trade": round(min(pips_list), 2) if pips_list else 0.0,
    }


def _grid_trade(row: sqlite3.Row) -> GridTradeRecord:
    return GridTradeRecord(
        id=row["id"],
        direction=row["direction"],
        entry_time=datetime.fromisoformat(row["entry_time"]),
        exit_time=datetime.fromisoformat(row["exit_time"]) if row["exit_time"] else None,
        entry_price=row["entry_price"],
        exit_price=row["exit_price"],
        tp_price=row["tp_price"],
        pips=row["pips"],
        lot_size=row["lot_size"],
        running_equity=row["running_equity"],
    )

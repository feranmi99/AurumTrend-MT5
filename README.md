# K9 Gold Bot — XAUUSD Trend-Capture Bot

EMA(9)/EMA(21) crossover bot with ADX filter and a trailing stop-only exit.
Runs as a FastAPI service connected to MetaTrader 5.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| **Windows 10/11** | The `MetaTrader5` Python package is Windows-only (wraps MT5 IPC). |
| **MT5 desktop installed** | Download from your broker or [metatrader5.com](https://www.metatrader5.com). |
| **MT5 terminal running and logged in** | The bot connects to the terminal process — it must be open. |
| **Python 3.11+** | `py -3.11` or `python` depending on your Windows Python install. |
| **Demo account** | Required for the initial validation phase (see Safety below). |

---

## Setup

### 1. Install dependencies

```powershell
pip install -r requirements.txt
```

### 2. Configure `.env`

```powershell
copy .env.example .env
```

Edit `.env` with your MT5 credentials and broker-specific symbol name.  
**Verify your symbol name in MT5 Market Watch** — many brokers use `XAUUSDm`, `XAUUSD.`, `GOLD`, etc.

**Verify pip size**: open MT5, check the quote price for XAUUSD. If it looks like `2350.45`, then 1 pip = `$0.01` (default). If it looks like `2350.4`, then 1 pip = `$0.1` — update `PIP_SIZE` accordingly.

### 3. Verify MT5 connection before running

In Python:

```python
import MetaTrader5 as mt5
mt5.initialize(login=YOUR_LOGIN, password="YOUR_PASSWORD", server="YOUR_SERVER")
print(mt5.account_info())
print(mt5.symbol_info_tick("XAUUSDm"))
mt5.shutdown()
```

Both calls must return non-None results before the bot will work.

### 4. Run the bot

```powershell
uvicorn main:app --host 0.0.0.0 --port 8000
```

The bot starts automatically when the FastAPI service starts.

---

## API endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/status` | Bot health, current position, account info |
| GET | `/trades` | All trade records (open + closed) |
| GET | `/config` | Current configuration |
| GET | `/tuning/pending` | ADX threshold suggestions awaiting approval |
| POST | `/tuning/approve/{id}` | Apply a pending tuning suggestion |

Interactive docs: `http://localhost:8000/docs`

---

## Strategy summary

- **Entry**: EMA(9) crosses EMA(21) with ADX(14) > threshold (default 20)
- **One position at a time** — no pyramiding, no auto-reverse
- **Stop logic**:
  - Initial fixed stop at `INITIAL_STOP_PIPS` from entry
  - Once in profit: trailing stop at `TRAIL_PIPS` from the most favorable price seen
  - Stop only tightens, never loosens
- **No take-profit** — the only exit is the stop

---

## Safety and demo-validation phase

The bot will **refuse to run on a live account** unless `I_UNDERSTAND_THIS_IS_LIVE=true` is set in `.env`.

Before considering live trading:

1. Run on a demo account for **at least 200 closed trades AND 4 weeks**, whichever is longer.
2. Export and analyze `trades.db` (open with any SQLite viewer, e.g. [DB Browser for SQLite](https://sqlitebrowser.org/)).
3. The backtest showed a ~3% win rate with rare large wins driving all profit — a small demo sample cannot confirm a real edge. 200+ trades on forward data is the minimum meaningful test.

This is not optional polish. Do not skip it.

---

## Self-tuning

After every `TUNING_REVIEW_EVERY_N_TRADES` (default 20) closed trades, the bot analyzes whether the ADX threshold should be raised:

- If trades taken near the current threshold have a >60% loss rate, a raise is suggested.
- The suggestion is logged with the exact numbers that drove it.
- With `AUTO_APPLY_TUNING=false` (default), the suggestion sits in `/tuning/pending` until you call `/tuning/approve/{id}`.
- With `AUTO_APPLY_TUNING=true`, it's applied immediately in memory. Update `ADX_THRESHOLD` in `.env` to persist across restarts.

---

## Trade log

All closed trades are written to `trades.db` (SQLite) and viewable via `/trades`.  
Each record includes: direction, entry/exit time, entry/exit price, exit reason (`initial_stop` / `trailing_stop`), pips P&L, ADX at entry, and running account equity.

Log file: `k9bot.log` (rotated manually).

---

## File structure

```
main.py          FastAPI app + bot loop
mt5_client.py    MT5 connection, orders, position management
strategy.py      EMA/ADX indicators, stop/trail state machine
tuning.py        ADX threshold self-tuning logic
trade_log.py     SQLite persistence
models.py        Pydantic models
backtest_engine.py  Offline parameter sweep (separate from live bot)
.env.example     Configuration template
requirements.txt
trades.db        Created on first run
k9bot.log        Created on first run
```

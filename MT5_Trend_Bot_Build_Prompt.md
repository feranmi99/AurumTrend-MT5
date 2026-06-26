# Build Prompt: Gold (XAUUSD) Trend-Capture Trading Bot

Paste everything below into a fresh chat with whatever AI coding agent you're using
(Claude, GPT, etc.) to build this. It's written to be self-contained.

---

## Context

I am building an automated trading bot for XAUUSD (gold) that connects to MetaTrader 5
(MT5) via Python. I need a FastAPI service that runs the bot logic, exposes a status/control
API, and logs everything for review. This is going live on a **demo account first** —
do not skip or shortcut the demo-validation phase described below, it is a hard requirement,
not optional polish.

## Strategy specification (implement exactly as described — do not "improve" the logic
without telling me first)

**Instrument**: XAUUSDm (note the lowercase "m" suffix — this is broker-specific, the
exact symbol name must be configurable via `.env`, not hardcoded)

**Entry signal**:
- EMA(9) crosses EMA(21) → directional signal (bullish cross = long, bearish cross = short)
- Filter: only take the signal if ADX(14) > threshold (default 20, must be configurable)
- One position open at a time. No pyramiding, no overlapping trades.
- After a position closes (by any stop), return to scanning for the next signal. Do NOT
  auto-reverse into a new trade immediately — wait for a fresh, independent signal.

**Exit logic — this is the core of the strategy, implement precisely**:
- On entry, set a fixed initial stop-loss at `INITIAL_STOP_PIPS` distance from entry
  (configurable, default 7 pips — this was the best-balanced value from backtesting,
  see Section "Backtest context" below).
- While the trade is NOT in profit (price hasn't moved beyond entry in the favorable
  direction), the stop stays FIXED at the initial level. It never widens.
- Once the trade moves into profit, the stop begins trailing behind price at
  `TRAIL_PIPS` distance (configurable, default 5 pips), updating on every new price tick
  or bar close. The trailing stop only ever tightens in the trade's favor — it must
  never move backward/loosen, even temporarily.
- There is NO take-profit target. The only exit is the stop-loss (initial or trailing)
  being hit.
- Use the bid/ask appropriately for direction (buy positions check against bid for stop
  hits, sell positions check against ask) — do not use mid-price for stop-hit detection,
  this matters for accuracy.

**Position sizing**: configurable lot size via `.env` (`LOT_SIZE`), default 0.01. Do not
implement martingale, position-size-doubling-after-loss, or any scaling that increases
risk after a loss. Risk per trade should be flat and predictable.

**Operating mode**: the bot scans continuously (poll MT5 price feed, e.g. every few
seconds or on each new M1/M5 bar close — your choice, but make the polling interval
configurable) but is NOT always in a position. It waits for a valid entry signal,
manages the trade per the exit logic above, then returns to scanning. It is not a
24/7-always-in-market system.

## Self-tuning component (scoped narrowly — read carefully)

Implement a simple, explainable adaptive layer, NOT a black-box ML model:

- Track the outcome of each closed trade (win/loss, pips, ADX value at entry, time of
  day, initial-stop-distance used).
- After every N closed trades (configurable, default 20), recalculate a suggested
  `ADX_THRESHOLD` by analyzing whether trades taken at higher ADX values had better
  expectancy than trades taken near the current threshold. If raising the threshold
  would have filtered out more losers than winners in the recent trade history, raise
  it by a small fixed step (e.g. +1). If lowering it would have let in more winners than
  losers, lower it by a small step. Cap adjustments to a configurable min/max range so
  it can't drift to an extreme.
- This adjustment must be **logged with the reasoning** (e.g. "ADX threshold raised from
  20 to 21: of the last 20 trades, trades with ADX 20-21 had a 65% loss rate") and must
  require a manual approval flag (`AUTO_APPLY_TUNING=false` by default) before being
  applied automatically. When false, the bot should log the suggested change but keep
  using the existing threshold until a human approves it via the API.
- Do NOT implement reinforcement learning, neural networks, or any system whose
  decision logic isn't traceable to a specific, loggable reason. The whole point of this
  layer is that I can read the log and understand exactly why a parameter changed.

## Mandatory demo-validation phase (do not skip this)

Before any live-money trading is enabled:

1. Build the bot to run against the **MT5 demo account only**. Read account type from
   MT5 (`mt5.account_info().trade_mode`) on startup and refuse to proceed if it detects
   a real/live account unless an explicit `I_UNDERSTAND_THIS_IS_LIVE=true` flag is set
   in `.env` — this is a deliberate safety gate, keep it.
2. Run on demo for a minimum of 200 closed trades or 4 weeks, whichever is longer,
   before even considering live deployment. This is a statistics requirement, not
   arbitrary caution — backtests on this strategy so far have shown winning trades are
   rare (single digits per 300+ trades) and large, so a small sample size cannot
   distinguish a real edge from a lucky run. 200+ trades on a forward-walking demo
   account, where the bot can't have been curve-fit to that specific data, is the
   actual test of whether this works.
3. Log every trade (entry time, exit time, direction, entry price, exit price, exit
   reason [initial_stop/trailing_stop], pips, running account equity) to a local
   SQLite or CSV log, not just console output, so I can analyze it afterward the same
   way we analyzed the backtest.
4. Expose a `/status` and `/trades` API endpoint so I can check bot health and trade
   history without digging through logs manually.

## Technical requirements

- **Python 3.11+, FastAPI** for the service layer
- **MetaTrader5 Python package** (`pip install MetaTrader5`) for the MT5 connection —
  note this package is Windows-only (wraps the MT5 terminal's IPC), so this must run on
  a Windows machine with MT5 desktop installed and logged in, not in a Linux container
- Read MT5 login credentials (`MT5_LOGIN`, `MT5_PASSWORD`, `MT5_SERVER`) and all strategy
  parameters from a `.env` file using `python-dotenv` — never hardcode credentials
- Structure:
  - `main.py` — FastAPI app, endpoints: `/status`, `/trades`, `/config` (GET current
    config), `/tuning/pending` (GET suggested-but-unapplied tuning changes),
    `/tuning/approve` (POST to apply a pending suggestion)
  - `mt5_client.py` — wraps all MT5 connection, symbol info, order placement, position
    modification (for the trailing stop), and position closing
  - `strategy.py` — EMA/ADX signal calculation and the stop/trail state machine, kept
    separate from the MT5 plumbing so the logic itself is unit-testable without a live
    MT5 connection
  - `tuning.py` — the self-tuning logic described above, operating on the trade log
  - `models.py` — Pydantic models for trades, config, API responses
  - `trade_log.py` — SQLite (or CSV, your choice) persistence for closed trades
  - `.env.example` — template showing all required variables with placeholder values,
    clearly comment that this is NOT the real `.env` and real credentials never get
    committed
  - `requirements.txt`
  - `README.md` — setup instructions specific to Windows + MT5 desktop, including how
    to verify the demo account is connected before running

## What NOT to do

- Do not implement auto-reverse (immediately opening an opposite trade when stopped out)
- Do not implement always-in-market logic — the bot must be flat between signals
- Do not implement martingale or any loss-triggered position-size increase
- Do not silently enable live trading — the safety gate in the demo-validation section
  must be a real, working check, not a comment or TODO
- Do not build the self-tuning layer as an opaque model — every parameter change must be
  traceable to a specific logged reason

## Backtest context (for the AI building this — informs sensible defaults, not requirements)

A backtest on ~3 months of XAUUSDm M1 data resampled to M5, using this exact entry/exit
logic, showed: at 5-pip initial stop / 5-pip trail, win rate ~2.9% (10 wins out of 344
trades), profit factor 1.75, but the result was driven almost entirely by 2 large trades
catching fast volatility spikes — removing those 2 trades made the result net negative.
This means the strategy's viability is NOT yet established; the demo-validation phase
above exists specifically to find out, on fresh forward data, whether this is a real,
repeatable edge or not. Build the bot to make that determination easy to read from the
logs, not to assume the strategy already works.

---

*End of build prompt.*
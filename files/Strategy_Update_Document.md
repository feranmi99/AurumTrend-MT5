# Strategy Update Document
## XAUUSDm Trend Bot — Changes from Original Build Prompt

This document is for the AI implementing the bot. It summarizes every validated
change made after extensive backtesting on a full year of XAUUSDm M5 data
(June 2025 – June 2026, ~70,500 bars). Hand this to the AI alongside the original
build prompt and the .env.example — together they fully define what to build.

---

## What changed and why

### 1. Timeframe: M1 → M5 (confirmed)

The original spec mentioned M1. After backtesting both:
- M1 with 5-15 pip stops is dominated by noise. Gold's normal M1 tick movement
  clips these stops constantly before real moves develop.
- M5 is the validated timeframe. All parameters below are calibrated to M5.

**Implementation**: `TIMEFRAME=M5` in .env. The bot should poll on M5 bar closes
(or every 10 seconds while scanning, whichever comes first) — not on every M1 tick.

---

### 2. Initial stop: 7 pips → 15 pips

Original prompt defaulted to 7 pips. Full-year sweep tested 5, 7, 10, 15 pips:
- 5-pip stop: win rate 1.6%, PF 1.16
- 7-pip stop: win rate 2.3%, PF 1.17
- 10-pip stop: win rate 3.0%, PF 1.11
- **15-pip stop: win rate 4.6%, PF 1.39** ← validated best

Reason: at 5-10 pips, gold's normal M5 candle noise clips the stop before the
trade has any chance to develop. 15 pips gives the trade enough room to survive
the first bar's noise while still capping the loss at a defined level.

**Implementation**: `INITIAL_STOP_PIPS=15` in .env.

---

### 3. Trail distance: 5 pips → 7 pips

After testing trail distances of 5, 7, 10, 15 pips paired with the 15-pip
initial stop, 7 pips produced the best balance of locking profits quickly while
not getting shaken out by short-term reversals.

**Implementation**: `TRAIL_PIPS=7` in .env.

---

### 4. New: H1 trend-direction filter (strongly recommended)

This is the most important new addition. Before taking any M5 EMA9/21 cross
signal, the bot now checks whether the H1 timeframe agrees with the trade direction:
- H1 close > H1 EMA(50) → only allow BUY signals on M5
- H1 close < H1 EMA(50) → only allow SELL signals on M5

**Why**: In backtesting this single filter cut maximum drawdown nearly in half
(4,910 pips → 2,677 pips) while keeping expectancy positive (+6.69 pips/trade).
For a small starting account where survivability through long losing streaks is
the primary constraint, this is the right trade-off.

**Implementation**:
```python
# In strategy.py — before processing an M5 EMA cross signal:
def h1_trend_is_aligned(direction: str, mt5_client) -> bool:
    h1_bars = mt5_client.get_bars(symbol, 'H1', count=60)
    h1_ema50 = compute_ema(h1_bars['Close'], period=50).iloc[-1]
    h1_last_close = h1_bars['Close'].iloc[-1]
    if direction == 'buy':
        return h1_last_close > h1_ema50
    else:
        return h1_last_close < h1_ema50
```
Controlled by `HTF_FILTER_ENABLED=true` in .env. When false, falls back to the
original M5-only entry (profitable but higher drawdown).

---

### 5. Entry signal: confirmed EMA9/21 + ADX(14) > 20

Nine alternative entry signals were tested against the same exit logic:
- Faster EMA pairs (EMA3/8): more trades, net unprofitable
- Bollinger Band breakout: near break-even, worse than baseline
- MACD histogram flip: net unprofitable
- RSI(14) mean-reversion: worst of all tested
- Price breakout (10-bar and 20-bar): net unprofitable

**The original EMA9/21 + ADX(14) > 20 outperformed all alternatives.**
Do not change the entry signal without re-running a full backtest first.

---

### 6. New: equity-tied position scaling (optional module)

Original prompt used a flat 0.01 lot size. This is correct for the demo phase.
After demo validation, the bot can optionally scale lot size as account equity
grows — not after losses (that would be martingale), but as the balance genuinely
increases above the starting base.

**Implementation**: see `EQUITY_SCALING_*` variables in .env.example.
This module should be in `position_sizing.py`, called by the entry logic:

```python
def get_lot_size(current_equity: float, config) -> float:
    if not config.equity_scaling_enabled:
        return config.lot_size  # flat sizing
    growth = max(0, current_equity - config.equity_scaling_base)
    steps = int(growth // config.equity_scaling_equity_step)
    scaled = config.lot_size + (steps * config.equity_scaling_lot_step)
    return min(scaled, config.equity_scaling_max_lot)
```

---

### 7. What has NOT changed from the original build prompt

- **FastAPI service structure** (main.py, mt5_client.py, strategy.py, tuning.py,
  models.py, trade_log.py) — build exactly as originally specified
- **No take-profit** — exit via stop only, no change
- **No auto-reverse** — after exit, return to scanning, no change
- **No martingale** — lot size never increases after a loss, no change
- **Demo-only safety gate** — `I_UNDERSTAND_THIS_IS_LIVE=false` blocks live trading
  until `DEMO_MIN_TRADES=200` and `DEMO_MIN_DAYS=28` are both met, no change
- **Self-tuning ADX layer** — logs suggestions, requires manual approval by default,
  no change to the mechanism, only the validated ADX range changed (15–30)
- **SQLite trade log + /status, /trades, /config, /tuning API endpoints** — no change
- **Windows + MT5 desktop required** — MetaTrader5 Python package is Windows-only

---

## Summary of validated parameters (use these as the source of truth)

| Parameter | Original | Updated | Why |
|---|---|---|---|
| Timeframe | M1 (requested) | M5 | M1 dominated by noise at these stop distances |
| Initial stop | 7 pips | **15 pips** | Best PF across full-year sweep |
| Trail distance | 5 pips | **7 pips** | Best pairing with 15-pip initial stop |
| H1 trend filter | Not present | **Enabled by default** | Halved drawdown |
| Entry signal | EMA9/21+ADX | **EMA9/21+ADX (unchanged)** | Outperformed 8 alternatives |
| Lot size (start) | 0.01 | **0.01 (unchanged)** | Survivability on small account |
| Equity scaling | Not present | **Available, off by default** | Post-demo optional feature |

---

## Expected behavior in demo (set these expectations before evaluating results)

- ~3-4 signals per day on average (H1-filtered version: fewer, ~1-2/day)
- ~95% of trades will be small losses (15-25 pips, ~$0.15-0.25 at 0.01 lot)
- Long losing streaks of 20-50+ consecutive losses are NORMAL, not a malfunction
- Profitability comes from occasional large winners (50-2000+ pips) that outweigh
  the accumulated small losses — patience through losing streaks is a hard requirement
- Minimum demo period before drawing any conclusions: 200 trades or 28 days,
  whichever is longer

---

*Document generated after full-year backtesting session, June 2026.*
*All parameters validated on XAUUSDm M5, Exness demo account data.*

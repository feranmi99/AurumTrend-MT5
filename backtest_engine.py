"""
Gold (XAUUSD) Stop-Loss-Only Trend Bot — Backtest Engine
==========================================================

STRATEGY BEING TESTED (as specified by user):
- Entry: EMA(9)/EMA(21) crossover, filtered by ADX trend strength
- Initial stop: fixed pip distance from entry (tested: 5, 7, 10 pips)
- NO take-profit target
- Trailing logic:
    * While trade is in LOSS (price between entry and initial stop): stop stays FIXED
      at the initial level. It does NOT widen.
    * Once trade moves into PROFIT: stop begins trailing behind price at a fixed
      pip distance, locking in gains. It only ever tightens in the trade's favor,
      never loosens.
- Exit ONLY via stop being hit (initial stop or trailing stop). No fixed TP.
- After exit: bot returns to scanning for next signal. No auto-reverse.

This script sweeps across:
- Timeframes: M1 (primary, as requested) and M5 (comparison, recommended by Claude
  because M1 + 5-10 pip stops on gold is likely to be dominated by noise)
- Initial stop distances: 5, 7, 10 pips
- Trail distances: 5, 7, 10 pips

Outputs per combination: total trades, win rate, profit factor, expectancy (in pips
and in account currency given a position size), max drawdown, largest win, largest loss.

IMPORTANT CAVEATS BUILT INTO THIS ENGINE (read before trusting the numbers):
1. "Pips" for XAUUSD: this script assumes 1 pip = $0.01 move (i.e. 2nd decimal),
   which is the common MT5 convention for gold where price is quoted like 2350.45.
   VERIFY this matches your broker's pip definition before trusting absolute pip
   counts — some brokers/EAs define a gold "pip" as $0.10. This is configurable
   below (PIP_SIZE).
2. Spread and slippage are NOT included in the raw OHLC backtest by default —
   they are added explicitly as a per-trade cost (SPREAD_PIPS) because they
   materially change results for a stop-loss-only strategy with no TP. Set this
   to your actual broker's typical XAUUSD spread (often 15-35 cents = 15-35 "pips"
   under this script's pip definition during normal hours, wider during news/low
   liquidity). Default below is a conservative placeholder — UPDATE IT once you
   know your broker's actual spread, or the results will be optimistic.
3. This is a SIGNAL-BAR backtest using OHLC bars, not true tick-level simulation.
   It approximates whether intra-bar price action would have hit the stop. For an
   M1 strategy this is reasonably tight; for confirming a final design before
   going live, a tick-level backtest (e.g. via MT5's own Strategy Tester) is the
   final word, not this script.
4. No commission is modeled. Add it via COMMISSION_PER_TRADE if your broker charges
   per-trade commission on top of spread (common with ECN/raw-spread accounts).
"""

import pandas as pd
import numpy as np
from dataclasses import dataclass, field
from typing import Optional
import itertools

# ============================================================
# CONFIGURATION — adjust these to match your actual broker
# ============================================================

PIP_SIZE = 0.01          # 1 pip = $0.01 move in XAUUSD price. CHANGE if your broker uses $0.10.
SPREAD_PIPS = 20         # Placeholder. Replace with your broker's typical XAUUSD spread in pips.
COMMISSION_PER_TRADE = 0.0  # In account currency, per round-trip trade. Set if applicable.
LOT_SIZE_UNITS = 100     # 1 standard lot XAUUSD = 100 oz. Used only for $ P&L conversion in summary.
POSITION_SIZE_LOTS = 0.01  # Used only to convert pip P&L into a $ estimate at the end.

INITIAL_STOP_OPTIONS_PIPS = [5, 7, 10]
TRAIL_STOP_OPTIONS_PIPS = [5, 7, 10]

EMA_FAST = 9
EMA_SLOW = 21
ADX_PERIOD = 14
ADX_THRESHOLD = 20   # Minimum trend strength to allow an entry. Tested as fixed; can be swept too.


# ============================================================
# INDICATOR CALCULATIONS
# ============================================================

def compute_ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def compute_adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Standard Wilder ADX calculation from High/Low/Close."""
    high = df['High']
    low = df['Low']
    close = df['Close']

    plus_dm = high.diff()
    minus_dm = -low.diff()

    plus_dm[plus_dm < 0] = 0
    minus_dm[minus_dm < 0] = 0
    # When both moves are positive, only the larger counts
    mask = plus_dm < minus_dm
    plus_dm[mask] = 0
    mask2 = minus_dm < plus_dm
    minus_dm[mask2] = 0

    tr1 = high - low
    tr2 = (high - close.shift()).abs()
    tr3 = (low - close.shift()).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    atr = tr.ewm(alpha=1/period, adjust=False).mean()
    plus_di = 100 * (plus_dm.ewm(alpha=1/period, adjust=False).mean() / atr)
    minus_di = 100 * (minus_dm.ewm(alpha=1/period, adjust=False).mean() / atr)

    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    adx = dx.ewm(alpha=1/period, adjust=False).mean()
    return adx


def prepare_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df['EMA_fast'] = compute_ema(df['Close'], EMA_FAST)
    df['EMA_slow'] = compute_ema(df['Close'], EMA_SLOW)
    df['ADX'] = compute_adx(df, ADX_PERIOD)

    # Crossover signals
    df['ema_diff'] = df['EMA_fast'] - df['EMA_slow']
    df['ema_diff_prev'] = df['ema_diff'].shift(1)

    df['bull_cross'] = (df['ema_diff_prev'] <= 0) & (df['ema_diff'] > 0)
    df['bear_cross'] = (df['ema_diff_prev'] >= 0) & (df['ema_diff'] < 0)

    return df


# ============================================================
# TRADE SIMULATION
# ============================================================

@dataclass
class Trade:
    direction: str          # 'buy' or 'sell'
    entry_price: float
    entry_time: object
    initial_stop: float
    exit_price: Optional[float] = None
    exit_time: Optional[object] = None
    exit_reason: Optional[str] = None   # 'initial_stop' or 'trailing_stop'
    max_favorable_pips: float = 0.0


def simulate(df: pd.DataFrame, initial_stop_pips: float, trail_pips: float,
             adx_threshold: float = ADX_THRESHOLD) -> list:
    """
    Walk through bars in time order. One trade open at a time (no pyramiding,
    no overlapping positions — matches the user's description of a single bot
    instance scanning, entering, managing, exiting, then scanning again).
    """
    trades = []
    in_position = False
    current: Optional[Trade] = None
    stop_level = None
    initial_stop_dist = initial_stop_pips * PIP_SIZE
    trail_dist = trail_pips * PIP_SIZE
    spread_cost = SPREAD_PIPS * PIP_SIZE

    for i in range(1, len(df)):
        row = df.iloc[i]

        if not in_position:
            if row['ADX'] < adx_threshold:
                continue
            if row['bull_cross']:
                entry_price = row['Close'] + (spread_cost / 2)  # pay half spread on entry (approx)
                current = Trade(
                    direction='buy',
                    entry_price=entry_price,
                    entry_time=row.name,
                    initial_stop=entry_price - initial_stop_dist
                )
                stop_level = current.initial_stop
                in_position = True
            elif row['bear_cross']:
                entry_price = row['Close'] - (spread_cost / 2)
                current = Trade(
                    direction='sell',
                    entry_price=entry_price,
                    entry_time=row.name,
                    initial_stop=entry_price + initial_stop_dist
                )
                stop_level = current.initial_stop
                in_position = True
            continue

        # --- In position: check stop hit using bar's High/Low, then update trail ---
        if current.direction == 'buy':
            # Did the stop get hit this bar? (use Low for buy)
            if row['Low'] <= stop_level:
                exit_price = stop_level - (spread_cost / 2)
                current.exit_price = exit_price
                current.exit_time = row.name
                current.exit_reason = 'trailing_stop' if stop_level > current.initial_stop else 'initial_stop'
                trades.append(current)
                in_position = False
                current = None
                stop_level = None
                continue

            # Update favorable excursion & trail
            favorable = row['High'] - current.entry_price
            current.max_favorable_pips = max(current.max_favorable_pips, favorable / PIP_SIZE)
            if favorable > 0:
                # In profit zone: trail stop up, never down
                candidate_stop = row['High'] - trail_dist
                if candidate_stop > stop_level:
                    stop_level = candidate_stop

        else:  # sell
            if row['High'] >= stop_level:
                exit_price = stop_level + (spread_cost / 2)
                current.exit_price = exit_price
                current.exit_time = row.name
                current.exit_reason = 'trailing_stop' if stop_level < current.initial_stop else 'initial_stop'
                trades.append(current)
                in_position = False
                current = None
                stop_level = None
                continue

            favorable = current.entry_price - row['Low']
            current.max_favorable_pips = max(current.max_favorable_pips, favorable / PIP_SIZE)
            if favorable > 0:
                candidate_stop = row['Low'] + trail_dist
                if candidate_stop < stop_level:
                    stop_level = candidate_stop

    return trades


# ============================================================
# METRICS
# ============================================================

def trade_pnl_pips(t: Trade) -> float:
    if t.direction == 'buy':
        return (t.exit_price - t.entry_price) / PIP_SIZE
    else:
        return (t.entry_price - t.exit_price) / PIP_SIZE


def summarize(trades: list, initial_stop_pips: float, trail_pips: float, timeframe: str) -> dict:
    if not trades:
        return {
            'timeframe': timeframe, 'initial_stop_pips': initial_stop_pips,
            'trail_pips': trail_pips, 'total_trades': 0, 'win_rate_pct': None,
            'profit_factor': None, 'expectancy_pips': None, 'expectancy_usd_est': None,
            'max_drawdown_pips': None, 'largest_win_pips': None, 'largest_loss_pips': None,
            'commission_modeled': COMMISSION_PER_TRADE
        }

    pnls = [trade_pnl_pips(t) - (COMMISSION_PER_TRADE / (PIP_SIZE * LOT_SIZE_UNITS * POSITION_SIZE_LOTS) if COMMISSION_PER_TRADE else 0) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    win_rate = len(wins) / len(pnls) * 100
    gross_win = sum(wins) if wins else 0
    gross_loss = abs(sum(losses)) if losses else 0
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else float('inf')
    expectancy = np.mean(pnls)

    # Drawdown on cumulative pip equity curve
    equity = np.cumsum(pnls)
    running_max = np.maximum.accumulate(equity) if len(equity) > 0 else np.array([0])
    drawdown = running_max - equity
    max_dd = drawdown.max() if len(drawdown) > 0 else 0

    usd_per_pip = PIP_SIZE * LOT_SIZE_UNITS * POSITION_SIZE_LOTS

    return {
        'timeframe': timeframe,
        'initial_stop_pips': initial_stop_pips,
        'trail_pips': trail_pips,
        'total_trades': len(trades),
        'win_rate_pct': round(win_rate, 1),
        'profit_factor': round(profit_factor, 2) if profit_factor != float('inf') else 'inf',
        'expectancy_pips': round(expectancy, 2),
        'expectancy_usd_est': round(expectancy * usd_per_pip, 2),
        'max_drawdown_pips': round(max_dd, 1),
        'largest_win_pips': round(max(pnls), 1) if pnls else None,
        'largest_loss_pips': round(min(pnls), 1) if pnls else None,
        'stopped_initial': sum(1 for t in trades if t.exit_reason == 'initial_stop'),
        'stopped_trailing_profit': sum(1 for t in trades if t.exit_reason == 'trailing_stop'),
    }


# ============================================================
# MAIN SWEEP
# ============================================================

def load_mt5_csv(path: str) -> pd.DataFrame:
    """
    Loads an MT5-exported CSV. Handles the common MT5 export formats:
    - Tab or comma separated
    - Columns: Date, Time, Open, High, Low, Close, [Volume/Tick Volume, Spread, Real Volume]
    """
    # Try comma first, fall back to tab
    try:
        df = pd.read_csv(path)
    except Exception:
        df = pd.read_csv(path, sep='\t')

    df.columns = [c.strip().replace('<', '').replace('>', '') for c in df.columns]

    # Normalize column names (MT5 sometimes exports as DATE, TIME, OPEN, etc. uppercase)
    col_map = {c.lower(): c for c in df.columns}
    rename = {}
    for target in ['date', 'time', 'open', 'high', 'low', 'close']:
        if target in col_map:
            rename[col_map[target]] = target.capitalize()
    df = df.rename(columns=rename)

    if 'Date' in df.columns and 'Time' in df.columns:
        df['datetime'] = pd.to_datetime(df['Date'].astype(str) + ' ' + df['Time'].astype(str), errors='coerce')
    elif 'Date' in df.columns:
        df['datetime'] = pd.to_datetime(df['Date'], errors='coerce')
    else:
        raise ValueError("Could not find Date/Time columns in CSV. Check export format.")

    df = df.dropna(subset=['datetime'])
    df = df.set_index('datetime').sort_index()
    df = df[['Open', 'High', 'Low', 'Close']].astype(float)
    return df


def resample_to_m5(df_m1: pd.DataFrame) -> pd.DataFrame:
    return df_m1.resample('5min').agg({
        'Open': 'first', 'High': 'max', 'Low': 'min', 'Close': 'last'
    }).dropna()


def run_full_sweep(csv_path: str) -> pd.DataFrame:
    print(f"Loading data from {csv_path} ...")
    df_m1 = load_mt5_csv(csv_path)
    print(f"Loaded {len(df_m1)} M1 bars from {df_m1.index.min()} to {df_m1.index.max()}")

    df_m5 = resample_to_m5(df_m1)
    print(f"Resampled to {len(df_m5)} M5 bars")

    results = []
    for timeframe, df_raw in [('M1', df_m1), ('M5', df_m5)]:
        df_ind = prepare_indicators(df_raw)
        for init_stop, trail in itertools.product(INITIAL_STOP_OPTIONS_PIPS, TRAIL_STOP_OPTIONS_PIPS):
            trades = simulate(df_ind, init_stop, trail)
            summary = summarize(trades, init_stop, trail, timeframe)
            results.append(summary)
            print(f"  [{timeframe}] stop={init_stop}p trail={trail}p -> "
                  f"trades={summary['total_trades']}, win%={summary['win_rate_pct']}, "
                  f"PF={summary['profit_factor']}, expectancy={summary['expectancy_pips']}p")

    return pd.DataFrame(results)


if __name__ == '__main__':
    import sys
    if len(sys.argv) < 2:
        print("Usage: python backtest_engine.py <path_to_mt5_export.csv>")
        sys.exit(1)

    results_df = run_full_sweep(sys.argv[1])
    out_path = 'backtest_results.csv'
    results_df.to_csv(out_path, index=False)
    print(f"\nFull results saved to {out_path}")
    print("\nTop 5 by profit factor (min 30 trades for statistical relevance):")
    valid = results_df[(results_df['total_trades'] >= 30) & (results_df['profit_factor'] != 'inf')]
    if len(valid) > 0:
        print(valid.sort_values('profit_factor', ascending=False).head(5).to_string(index=False))
    else:
        print("No parameter combination produced 30+ trades — see full CSV for details.")
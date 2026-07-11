"""
Full backtest sweep across all available data files.

Datasets tested:
  1. 1-year M1 data  →  simulate on M1 bars
  2. 1-year M1 data  →  resampled to M5, simulate on M5 bars
  3. 1-year M5 data  →  simulate directly on M5 bars (no resampling)

Results saved to backtest_results_full.csv
"""
import itertools
import os
import sys

import pandas as pd

# Import strategy helpers from the existing backtest engine
sys.path.insert(0, os.path.dirname(__file__))
from backtest_engine import (
    INITIAL_STOP_OPTIONS_PIPS,
    TRAIL_STOP_OPTIONS_PIPS,
    HTF_FILTER_OPTIONS,
    ADX_THRESHOLD,
    load_mt5_csv,
    resample_to_m5,
    prepare_indicators,
    simulate,
    summarize,
)

BASE = os.path.dirname(__file__)

M1_1YR  = os.path.join(BASE, 'data', '1782000742730_XAUUSDm_M1_1yr_export.csv')
M5_1YR  = os.path.join(BASE, 'data', '1782000629801_XAUUSDm_M5_1yr_export.csv')


def sweep(label: str, df_raw: pd.DataFrame) -> list:
    df_ind = prepare_indicators(df_raw)
    rows = []
    for init_stop, trail, htf_enabled in itertools.product(INITIAL_STOP_OPTIONS_PIPS, TRAIL_STOP_OPTIONS_PIPS, HTF_FILTER_OPTIONS):
        trades = simulate(df_ind, init_stop, trail, htf_filter_enabled=htf_enabled)
        current_label = f"{label} (HTF={'ON' if htf_enabled else 'OFF'})"
        summary = summarize(trades, init_stop, trail, current_label)
        rows.append(summary)
        print(
            f"  [{current_label}] stop={init_stop}p trail={trail}p → "
            f"trades={summary['total_trades']}, "
            f"win%={summary['win_rate_pct']}, "
            f"PF={summary['profit_factor']}, "
            f"expectancy={summary['expectancy_pips']}p"
        )
    return rows


def main():
    all_rows = []

    # ── 1-year M1 data ───────────────────────────────────────
    print(f"\nLoading 1-year M1 data: {os.path.basename(M1_1YR)}")
    df_m1 = load_mt5_csv(M1_1YR)
    print(f"  {len(df_m1)} M1 bars  {df_m1.index.min()} → {df_m1.index.max()}")

    all_rows += sweep('M1 (1yr)', df_m1)

    df_m5_from_m1 = resample_to_m5(df_m1)
    print(f"\n  Resampled to {len(df_m5_from_m1)} M5 bars")
    all_rows += sweep('M5-resampled (1yr)', df_m5_from_m1)

    # ── 1-year M5 data (direct) ──────────────────────────────
    print(f"\nLoading 1-year M5 data: {os.path.basename(M5_1YR)}")
    df_m5 = load_mt5_csv(M5_1YR)
    print(f"  {len(df_m5)} M5 bars  {df_m5.index.min()} → {df_m5.index.max()}")
    all_rows += sweep('M5-direct (1yr)', df_m5)

    # ── Save & summarise ─────────────────────────────────────
    results = pd.DataFrame(all_rows)
    out = os.path.join(BASE, 'backtest_results_full.csv')
    results.to_csv(out, index=False)
    print(f"\nAll results → {out}")

    print("\n── Top 10 by profit factor (≥30 trades) ──────────────")
    valid = results[
        (results['total_trades'] >= 30) &
        (results['profit_factor'] != 'inf') &
        (results['profit_factor'].notna())
    ].copy()
    valid['profit_factor'] = valid['profit_factor'].astype(float)

    cols = ['timeframe', 'initial_stop_pips', 'trail_pips', 'total_trades',
            'win_rate_pct', 'profit_factor', 'expectancy_pips',
            'max_drawdown_pips', 'largest_win_pips', 'stopped_trailing_profit']
    if len(valid):
        print(valid.sort_values('profit_factor', ascending=False).head(10)[cols].to_string(index=False))
    else:
        print("No combination hit 30+ trades — check CSV for full details.")

    print("\n── M5 results only (recommended timeframe) ───────────")
    m5 = valid[valid['timeframe'].str.startswith('M5')].sort_values('profit_factor', ascending=False)
    if len(m5):
        print(m5[cols].to_string(index=False))


if __name__ == '__main__':
    main()

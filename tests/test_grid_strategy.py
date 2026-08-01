"""
Unit tests for the K9 Grid Strategy Engine.

Tests cover:
- Market regime detection
- ATR-based grid spacing calculation
- Grid level generation
- Grid recentering logic
- Regime-adjusted level counts
"""
import pytest
import pandas as pd
import numpy as np

from grid_strategy import (
    MarketRegime,
    GridLevel,
    GridLevelStatus,
    detect_regime,
    calculate_grid_spacing,
    compute_atr,
    compute_adx,
    compute_indicators,
    generate_grid,
    should_recenter_grid,
    levels_for_regime,
)


# ── Market Regime Detection ───────────────────────────────────

class TestDetectRegime:
    def test_ranging_below_threshold(self):
        assert detect_regime(15.0) == MarketRegime.RANGING

    def test_ranging_at_zero(self):
        assert detect_regime(0.0) == MarketRegime.RANGING

    def test_neutral_between_thresholds(self):
        assert detect_regime(22.0) == MarketRegime.NEUTRAL

    def test_neutral_at_range_threshold(self):
        assert detect_regime(20.0) == MarketRegime.NEUTRAL

    def test_trending_above_threshold(self):
        assert detect_regime(30.0) == MarketRegime.TRENDING

    def test_trending_at_trend_threshold(self):
        # At exactly 25.0, ADX > 25 is False, so it should be NEUTRAL
        assert detect_regime(25.0) == MarketRegime.NEUTRAL

    def test_trending_just_above(self):
        assert detect_regime(25.1) == MarketRegime.TRENDING

    def test_custom_thresholds(self):
        assert detect_regime(10.0, range_threshold=15.0, trend_threshold=30.0) == MarketRegime.RANGING
        assert detect_regime(20.0, range_threshold=15.0, trend_threshold=30.0) == MarketRegime.NEUTRAL
        assert detect_regime(35.0, range_threshold=15.0, trend_threshold=30.0) == MarketRegime.TRENDING


# ── Grid Spacing Calculation ──────────────────────────────────

class TestCalculateGridSpacing:
    def test_normal_atr_gives_clamped_spacing(self):
        # ATR = 0.07, pip_size = 0.01, multiplier = 1.0
        # raw_pips = 0.07 / 0.01 = 7.0 → within [5, 10] → spacing = 7 * 0.01 = 0.07
        spacing = calculate_grid_spacing(0.07, pip_size=0.01)
        assert spacing == pytest.approx(0.07)

    def test_low_atr_gets_clamped_to_min(self):
        # ATR = 0.02, raw_pips = 2.0 → clamped to 5.0 → spacing = 0.05
        spacing = calculate_grid_spacing(0.02, pip_size=0.01)
        assert spacing == pytest.approx(0.05)

    def test_high_atr_gets_clamped_to_max(self):
        # ATR = 0.15, raw_pips = 15.0 → clamped to 10.0 → spacing = 0.10
        spacing = calculate_grid_spacing(0.15, pip_size=0.01)
        assert spacing == pytest.approx(0.10)

    def test_multiplier_scales_spacing(self):
        # ATR = 0.05, multiplier = 1.5 → raw = 0.075 → 7.5 pips → 0.075
        spacing = calculate_grid_spacing(0.05, pip_size=0.01, multiplier=1.5)
        assert spacing == pytest.approx(0.075)

    def test_custom_min_max(self):
        # ATR = 0.03 → 3 pips → below min 8 → clamped to 8 → 0.08
        spacing = calculate_grid_spacing(
            0.03, pip_size=0.01, min_spacing_pips=8.0, max_spacing_pips=15.0
        )
        assert spacing == pytest.approx(0.08)


# ── Grid Generation ──────────────────────────────────────────

class TestGenerateGrid:
    def test_generates_correct_number_of_levels(self):
        levels = generate_grid(
            center_price=2350.00,
            spacing=0.07,
            levels_above=5,
            levels_below=5,
            tp_pips=7.0,
            pip_size=0.01,
            lot_size=0.01,
        )
        assert len(levels) == 10

    def test_sell_levels_above_center(self):
        levels = generate_grid(
            center_price=2350.00,
            spacing=0.07,
            levels_above=3,
            levels_below=0,
            tp_pips=7.0,
            pip_size=0.01,
            lot_size=0.01,
        )
        assert len(levels) == 3
        for lv in levels:
            assert lv.direction == "sell"
            assert lv.order_price > 2350.00
            assert lv.tp_price < lv.order_price  # Sell TP is below entry

    def test_buy_levels_below_center(self):
        levels = generate_grid(
            center_price=2350.00,
            spacing=0.07,
            levels_above=0,
            levels_below=3,
            tp_pips=7.0,
            pip_size=0.01,
            lot_size=0.01,
        )
        assert len(levels) == 3
        for lv in levels:
            assert lv.direction == "buy"
            assert lv.order_price < 2350.00
            assert lv.tp_price > lv.order_price  # Buy TP is above entry

    def test_spacing_between_levels(self):
        levels = generate_grid(
            center_price=2350.00,
            spacing=0.10,
            levels_above=3,
            levels_below=0,
            tp_pips=7.0,
            pip_size=0.01,
            lot_size=0.01,
        )
        prices = sorted([lv.order_price for lv in levels])
        for i in range(1, len(prices)):
            assert prices[i] - prices[i - 1] == pytest.approx(0.10)

    def test_tp_distance_matches_config(self):
        levels = generate_grid(
            center_price=2350.00,
            spacing=0.07,
            levels_above=2,
            levels_below=2,
            tp_pips=5.0,
            pip_size=0.01,
            lot_size=0.01,
        )
        tp_dist = 5.0 * 0.01  # 0.05
        for lv in levels:
            if lv.direction == "sell":
                assert lv.order_price - lv.tp_price == pytest.approx(tp_dist)
            else:
                assert lv.tp_price - lv.order_price == pytest.approx(tp_dist)

    def test_all_levels_start_as_pending(self):
        levels = generate_grid(
            center_price=2350.00,
            spacing=0.07,
            levels_above=5,
            levels_below=5,
            tp_pips=7.0,
            pip_size=0.01,
            lot_size=0.01,
        )
        for lv in levels:
            assert lv.status == GridLevelStatus.PENDING

    def test_empty_grid_when_zero_levels(self):
        levels = generate_grid(
            center_price=2350.00,
            spacing=0.07,
            levels_above=0,
            levels_below=0,
            tp_pips=7.0,
            pip_size=0.01,
            lot_size=0.01,
        )
        assert len(levels) == 0


# ── Grid Recentering ─────────────────────────────────────────

class TestShouldRecenterGrid:
    def test_no_recenter_when_close(self):
        assert not should_recenter_grid(
            current_price=2350.10,
            grid_center=2350.00,
            spacing=0.07,
            threshold_levels=3,
        )

    def test_recenter_when_drifted_far(self):
        # Drift = 0.25, threshold = 3 * 0.07 = 0.21 → should recenter
        assert should_recenter_grid(
            current_price=2350.25,
            grid_center=2350.00,
            spacing=0.07,
            threshold_levels=3,
        )

    def test_recenter_works_both_directions(self):
        assert should_recenter_grid(
            current_price=2349.75,
            grid_center=2350.00,
            spacing=0.07,
            threshold_levels=3,
        )

    def test_exact_threshold_triggers_recenter(self):
        # Drift = 0.21 == threshold = 3 * 0.07 = 0.21
        assert should_recenter_grid(
            current_price=2350.21,
            grid_center=2350.00,
            spacing=0.07,
            threshold_levels=3,
        )


# ── Regime-Adjusted Levels ────────────────────────────────────

class TestLevelsForRegime:
    def test_ranging_full_levels(self):
        above, below = levels_for_regime(MarketRegime.RANGING, 5, 5)
        assert above == 5
        assert below == 5

    def test_neutral_half_levels(self):
        above, below = levels_for_regime(MarketRegime.NEUTRAL, 5, 5)
        assert above == 2  # 5 // 2 = 2
        assert below == 2

    def test_neutral_min_one_level(self):
        above, below = levels_for_regime(MarketRegime.NEUTRAL, 1, 1)
        assert above == 1  # max(1, 0) = 1
        assert below == 1

    def test_trending_no_levels(self):
        above, below = levels_for_regime(MarketRegime.TRENDING, 5, 5)
        assert above == 0
        assert below == 0


# ── Indicator Computation ─────────────────────────────────────

class TestIndicators:
    @pytest.fixture
    def sample_df(self):
        """Create a simple DataFrame with enough bars for ATR/ADX calculation."""
        np.random.seed(42)
        n = 50
        close = 2350.0 + np.cumsum(np.random.randn(n) * 0.5)
        high = close + np.abs(np.random.randn(n) * 0.3)
        low = close - np.abs(np.random.randn(n) * 0.3)
        return pd.DataFrame({
            "High": high,
            "Low": low,
            "Close": close,
        })

    def test_compute_atr_returns_series(self, sample_df):
        atr = compute_atr(sample_df, period=14)
        assert isinstance(atr, pd.Series)
        assert len(atr) == len(sample_df)
        assert atr.iloc[-1] > 0

    def test_compute_adx_returns_series(self, sample_df):
        adx = compute_adx(sample_df, period=14)
        assert isinstance(adx, pd.Series)
        assert len(adx) == len(sample_df)
        assert adx.iloc[-1] >= 0

    def test_compute_indicators_from_rates(self):
        """Test compute_indicators with rate-style dicts."""
        rates = []
        price = 2350.0
        for i in range(50):
            price += np.random.randn() * 0.5
            rates.append({
                "time": 1000 + i * 300,
                "open": price - 0.1,
                "high": price + 0.3,
                "low": price - 0.3,
                "close": price,
            })
        result = compute_indicators(rates)
        assert "atr" in result
        assert "adx" in result
        assert "close" in result
        assert result["atr"] > 0

    def test_compute_indicators_too_few_bars(self):
        rates = [
            {"time": 1, "open": 2350, "high": 2351, "low": 2349, "close": 2350},
            {"time": 2, "open": 2350, "high": 2351, "low": 2349, "close": 2350.5},
        ]
        result = compute_indicators(rates)
        assert result["atr"] == 0.0
        assert result["adx"] == 0.0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

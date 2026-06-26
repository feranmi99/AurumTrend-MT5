"""
Self-tuning: analyzes closed trade history to suggest ADX threshold adjustments.

Every decision is logged with an explicit, human-readable reason.
No black-box models — every suggested change traces to a specific data observation.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

import trade_log
from models import TuningSuggestion

log = logging.getLogger(__name__)


def analyze_and_suggest(
    current_threshold: float,
    threshold_min: float,
    threshold_max: float,
    tune_step: float,
    n_trades: int,
) -> Optional[TuningSuggestion]:
    """
    Look at the last n_trades closed trades and decide whether the ADX threshold
    should be raised.

    Logic (raising only — we cannot evaluate lowering because we have no data on
    trades that were filtered out below the threshold):

    - Split recent trades into "marginal" (ADX between current threshold and
      current_threshold + tune_step) and "strong" (ADX >= current_threshold + tune_step).
    - If marginal trades have a loss rate > 60% AND at least 3 marginal trades exist,
      raising the threshold would remove more losers than winners → suggest raising.
    - Alternatively, if strong trades win at >1.5× the marginal win rate, that also
      supports raising.

    All decisions are logged with the exact numbers that drove them.
    """
    trades = trade_log.get_closed_trades()
    if len(trades) < n_trades:
        log.info("Tuning: only %d trades in history, need %d.", len(trades), n_trades)
        return None

    recent = trades[-n_trades:]
    total_wins = sum(1 for t in recent if (t.pips or 0) > 0)
    overall_win_pct = 100 * total_wins // n_trades

    marginal = [t for t in recent if current_threshold <= t.adx_at_entry < current_threshold + tune_step]
    strong = [t for t in recent if t.adx_at_entry >= current_threshold + tune_step]

    log.info(
        "Tuning review — last %d trades: %d wins (%d%%). "
        "Marginal (ADX %.0f–%.0f): %d trades. Strong (ADX >%.0f): %d trades.",
        n_trades, total_wins, overall_win_pct,
        current_threshold, current_threshold + tune_step, len(marginal),
        current_threshold + tune_step, len(strong),
    )

    if not marginal:
        log.info("Tuning: no marginal trades found — no suggestion.")
        return None

    m_wins = sum(1 for t in marginal if (t.pips or 0) > 0)
    m_losses = len(marginal) - m_wins
    m_loss_rate = m_losses / len(marginal)

    parts = [
        f"Last {n_trades} trades: {total_wins} wins ({overall_win_pct}% win rate).",
        f"Marginal trades (ADX {current_threshold:.0f}–{current_threshold + tune_step:.0f}): "
        f"{len(marginal)} trades — {m_wins} wins, {m_losses} losses "
        f"({100 * m_loss_rate:.0f}% loss rate).",
    ]

    raise_by_loss_rate = m_loss_rate > 0.60 and len(marginal) >= 3
    raise_by_gap = False

    if strong:
        s_wins = sum(1 for t in strong if (t.pips or 0) > 0)
        s_win_rate = s_wins / len(strong)
        m_win_rate = m_wins / len(marginal)
        raise_by_gap = len(marginal) >= 3 and s_win_rate > m_win_rate * 1.5
        parts.append(
            f"Strong trades (ADX >{current_threshold + tune_step:.0f}): "
            f"{len(strong)} trades, {100 * s_win_rate:.0f}% win rate "
            f"vs {100 * m_win_rate:.0f}% for marginal."
        )

    if not (raise_by_loss_rate or raise_by_gap):
        log.info("Tuning: conditions not met for a change. %s", " ".join(parts))
        return None

    if current_threshold + tune_step > threshold_max:
        log.info("Tuning: would raise threshold but already at max (%.0f).", threshold_max)
        return None

    if raise_by_loss_rate:
        parts.append(
            f"Raising threshold from {current_threshold:.0f} to "
            f"{current_threshold + tune_step:.0f}: filtering marginal ADX band would "
            f"remove {m_losses} losers vs {m_wins} winners ({100 * m_loss_rate:.0f}% loss rate > 60% cutoff)."
        )
    else:
        parts.append(
            f"Raising threshold from {current_threshold:.0f} to "
            f"{current_threshold + tune_step:.0f}: strong-ADX trades win at >1.5× "
            "the rate of marginal trades."
        )

    reasoning = " ".join(parts)
    suggestion = TuningSuggestion(
        created_at=datetime.utcnow(),
        current_threshold=current_threshold,
        suggested_threshold=current_threshold + tune_step,
        reasoning=reasoning,
    )
    log.info(
        "Tuning suggestion: raise ADX threshold %.1f → %.1f. Reason: %s",
        current_threshold, current_threshold + tune_step, reasoning,
    )
    return suggestion

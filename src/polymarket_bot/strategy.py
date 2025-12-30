from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

from polymarket_bot.state import Position


@dataclass
class StrategyParams:
    chunk_stake: float
    trigger_below_cents: float
    dca_step_cents: float
    hedge_sum_under_cents: float
    max_stake_per_event: float


def choose_entry_side(up_px: float, down_px: float, trigger_below_cents: float) -> Optional[str]:
    both_below = (up_px < trigger_below_cents) and (down_px < trigger_below_cents)
    if both_below:
        return "up" if up_px <= down_px else "down"
    if up_px < trigger_below_cents:
        return "up"
    if down_px < trigger_below_cents:
        return "down"
    return None


def should_dca(main: Position, main_signal_px: float, step_cents: float) -> bool:
    if main.last_add_price_cents is None:
        return False
    return main_signal_px <= (main.last_add_price_cents - step_cents)


def should_hedge(main: Position, opp_signal_px: float, hedge_sum_under_cents: float) -> Tuple[bool, Optional[float]]:
    main_avg = main.avg_entry_cents()
    if main_avg is None:
        return False, None
    return (main_avg + opp_signal_px) < hedge_sum_under_cents, (main_avg + opp_signal_px)

from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict
from typing import Any, Dict, Optional


def _safe_float(x: Any) -> Optional[float]:
    try:
        if x is None:
            return None
        return float(x)
    except Exception:
        return None


@dataclass
class Position:
    side: str
    token_id: str
    total_stake_usd: float = 0.0
    total_shares: float = 0.0
    last_add_price_cents: Optional[float] = None
    first_entry_ts: Optional[str] = None
    last_add_ts: Optional[str] = None

    def avg_entry_cents(self) -> Optional[float]:
        if self.total_shares <= 0:
            return None
        return (self.total_stake_usd / self.total_shares) * 100.0

    def record_fill(self, cost_usd: float, shares: float, signal_price_cents: float, ts: str) -> None:
        self.total_stake_usd += float(cost_usd)
        self.total_shares += float(shares)
        self.last_add_price_cents = float(signal_price_cents)
        self.last_add_ts = ts
        if self.first_entry_ts is None:
            self.first_entry_ts = ts


@dataclass
class BotState:
    current_slug: Optional[str] = None
    current_condition_id: Optional[str] = None
    end_date_iso: Optional[str] = None
    up_token_id: Optional[str] = None
    down_token_id: Optional[str] = None

    main: Optional[Position] = None
    hedge: Optional[Position] = None
    hedged_ts: Optional[str] = None
    sum_avg_at_hedge: Optional[float] = None
    last_order_ts: Optional[str] = None

    def to_json(self) -> Dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_json(d: Dict[str, Any]) -> "BotState":
        st = BotState()
        st.current_slug = d.get("current_slug")
        st.current_condition_id = d.get("current_condition_id")
        st.end_date_iso = d.get("end_date_iso")
        st.up_token_id = d.get("up_token_id")
        st.down_token_id = d.get("down_token_id")
        st.hedged_ts = d.get("hedged_ts")
        st.sum_avg_at_hedge = _safe_float(d.get("sum_avg_at_hedge"))
        st.last_order_ts = d.get("last_order_ts")

        def pos_from(p: Any) -> Optional[Position]:
            if not isinstance(p, dict):
                return None
            return Position(
                side=str(p.get("side")),
                token_id=str(p.get("token_id")),
                total_stake_usd=float(p.get("total_stake_usd", 0.0)),
                total_shares=float(p.get("total_shares", 0.0)),
                last_add_price_cents=_safe_float(p.get("last_add_price_cents")),
                first_entry_ts=p.get("first_entry_ts"),
                last_add_ts=p.get("last_add_ts"),
            )

        st.main = pos_from(d.get("main"))
        st.hedge = pos_from(d.get("hedge"))
        return st


def load_state(path: str) -> BotState:
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return BotState.from_json(json.load(f))
    except Exception:
        pass
    return BotState()


def save_state(path: str, state: BotState) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state.to_json(), f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

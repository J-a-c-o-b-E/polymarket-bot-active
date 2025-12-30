from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import MarketOrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY


def safe_float(x: Any) -> Optional[float]:
    try:
        if x is None:
            return None
        return float(x)
    except Exception:
        return None


def _levels_from_book(book: Any, side: str) -> List[Dict[str, Any]]:
    if hasattr(book, side):
        return list(getattr(book, side) or [])
    if isinstance(book, dict) and side in book:
        return list(book.get(side) or [])
    raise RuntimeError("unknown order book format")


def vwap_cents_for_shares(client: ClobClient, token_id: str, shares: float) -> Optional[float]:
    book = client.get_order_book(token_id)
    asks = _levels_from_book(book, "asks")
    need = float(shares)
    if need <= 0:
        return None

    got = 0.0
    cost = 0.0
    for lvl in asks:
        p = safe_float(lvl.get("price") if isinstance(lvl, dict) else None)
        s = safe_float(lvl.get("size") if isinstance(lvl, dict) else None)
        if p is None or s is None or p <= 0 or s <= 0:
            continue
        take = min(s, need - got)
        cost += take * p
        got += take
        if got >= need - 1e-9:
            break

    if got < need - 1e-6:
        return None

    return (cost / got) * 100.0


def vwap_cents_for_usd(client: ClobClient, token_id: str, usd: float) -> Optional[float]:
    book = client.get_order_book(token_id)
    asks = _levels_from_book(book, "asks")
    budget = float(usd)
    if budget <= 0:
        return None

    spent = 0.0
    shares = 0.0
    for lvl in asks:
        p = safe_float(lvl.get("price") if isinstance(lvl, dict) else None)
        s = safe_float(lvl.get("size") if isinstance(lvl, dict) else None)
        if p is None or s is None or p <= 0 or s <= 0:
            continue

        lvl_cost = s * p
        if spent + lvl_cost <= budget + 1e-12:
            spent += lvl_cost
            shares += s
        else:
            remaining = budget - spent
            take = remaining / p
            if take <= 0:
                break
            shares += take
            spent += take * p
            break

        if spent >= budget - 1e-9:
            break

    if spent < budget - 1e-6 or shares <= 0:
        return None

    return (spent / shares) * 100.0


def place_market_buy(
    client: ClobClient,
    token_id: str,
    usd_amount: float,
    dry_run: bool,
) -> Optional[Tuple[float, float, float, Dict[str, Any]]]:
    amt = round(float(usd_amount), 2)
    if amt <= 0:
        return None

    if dry_run:
        px = client.get_price(token_id, side="BUY")
        px_f = safe_float(px)
        if px_f is None or px_f <= 0:
            return None
        shares = amt / px_f
        avg_cents = (amt / shares) * 100.0
        return (amt, shares, avg_cents, {"dry_run": True})

    mo = MarketOrderArgs(token_id=token_id, amount=amt, side=BUY, order_type=OrderType.FOK)
    signed = client.create_market_order(mo)
    resp = client.post_order(signed, OrderType.FOK)

    if not isinstance(resp, dict) or not resp.get("success"):
        return None

    making = safe_float(resp.get("makingAmount"))
    taking = safe_float(resp.get("takingAmount"))

    if making is None or taking is None or making <= 0 or taking <= 0:
        return None

    avg_price_cents = (making / taking) * 100.0
    return (making, taking, avg_price_cents, resp)

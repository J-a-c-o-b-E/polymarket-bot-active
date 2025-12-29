#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple, List

import requests

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import MarketOrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY


GAMMA_DEFAULT = "https://gamma-api.polymarket.com"
CLOB_HOST_DEFAULT = "https://clob.polymarket.com"
POLYGON_CHAIN_ID = 137

STATE_FILE_DEFAULT = "live_bot_state.json"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_iso(dt_str: str) -> datetime:
    # handles "Z"
    if dt_str.endswith("Z"):
        dt_str = dt_str[:-1] + "+00:00"
    return datetime.fromisoformat(dt_str)


def safe_float(x: Any) -> Optional[float]:
    try:
        if x is None:
            return None
        return float(x)
    except Exception:
        return None


def jloads_maybe(x: Any) -> Any:
    # gamma sometimes returns json-encoded strings for arrays
    if isinstance(x, str):
        s = x.strip()
        if (s.startswith("[") and s.endswith("]")) or (s.startswith("{") and s.endswith("}")):
            try:
                return json.loads(s)
            except Exception:
                return x
    return x


def get_env(name: str, default: Optional[str] = None) -> Optional[str]:
    v = os.environ.get(name)
    return v if v is not None and v != "" else default


@dataclass
class Position:
    side: str  # "up" | "down"
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
        d = asdict(self)
        # dataclass nested conversion already handled by asdict
        return d

    @staticmethod
    def from_json(d: Dict[str, Any]) -> "BotState":
        st = BotState()
        st.current_slug = d.get("current_slug")
        st.current_condition_id = d.get("current_condition_id")
        st.end_date_iso = d.get("end_date_iso")
        st.up_token_id = d.get("up_token_id")
        st.down_token_id = d.get("down_token_id")
        st.hedged_ts = d.get("hedged_ts")
        st.sum_avg_at_hedge = d.get("sum_avg_at_hedge")
        st.last_order_ts = d.get("last_order_ts")

        def pos_from(p: Any) -> Optional[Position]:
            if not isinstance(p, dict):
                return None
            return Position(
                side=p.get("side"),
                token_id=p.get("token_id"),
                total_stake_usd=float(p.get("total_stake_usd", 0.0)),
                total_shares=float(p.get("total_shares", 0.0)),
                last_add_price_cents=safe_float(p.get("last_add_price_cents")),
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
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state.to_json(), f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def gamma_list_markets(
    gamma_url: str,
    limit: int = 200,
    offset: int = 0,
    order: str = "endDate",
    ascending: bool = True,
) -> List[Dict[str, Any]]:
    url = gamma_url.rstrip("/") + "/markets"
    params = {
        "limit": limit,
        "offset": offset,
        "order": order,
        "ascending": "true" if ascending else "false",
    }
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, list):
        raise RuntimeError(f"Unexpected Gamma response type: {type(data)}")
    return data


def pick_current_market(markets: List[Dict[str, Any]], slug_prefixes: List[str]) -> Optional[Dict[str, Any]]:
    now = utc_now()
    for m in markets:
        slug = (m.get("slug") or "").strip()
        if not slug:
            continue
        if not any(slug.startswith(p) for p in slug_prefixes):
            continue

        start_s = m.get("startDate")
        end_s = m.get("endDate")
        if not start_s or not end_s:
            continue
        try:
            start = parse_iso(start_s)
            end = parse_iso(end_s)
        except Exception:
            continue

        if start <= now < end:
            return m
    return None


def extract_up_down_tokens_from_gamma_market(m: Dict[str, Any]) -> Tuple[str, str, str, str]:
    """
    Returns (condition_id, end_date_iso, up_token_id, down_token_id)
    """
    condition_id = str(m.get("conditionId") or "")
    if not condition_id:
        raise RuntimeError("Gamma market missing conditionId")

    end_date_iso = str(m.get("endDate") or "")
    if not end_date_iso:
        raise RuntimeError("Gamma market missing endDate")

    outcomes = jloads_maybe(m.get("outcomes"))
    clob_token_ids = jloads_maybe(m.get("clobTokenIds"))

    if not isinstance(outcomes, list) or not isinstance(clob_token_ids, list):
        raise RuntimeError("Gamma market missing outcomes/clobTokenIds arrays")

    if len(outcomes) != len(clob_token_ids):
        raise RuntimeError("Gamma market outcomes and clobTokenIds length mismatch")

    mapping = {str(o).strip().lower(): str(t) for o, t in zip(outcomes, clob_token_ids)}

    up = mapping.get("up")
    down = mapping.get("down")
    if not up or not down:
        # fallback: try substring match
        up = None
        down = None
        for o, t in zip(outcomes, clob_token_ids):
            ol = str(o).strip().lower()
            if up is None and "up" == ol:
                up = str(t)
            if down is None and "down" == ol:
                down = str(t)
        if not up or not down:
            raise RuntimeError(f"Could not find Up/Down token IDs in outcomes={outcomes}")

    return condition_id, end_date_iso, up, down


def _levels_from_book(book: Any, side: str) -> List[Dict[str, Any]]:
    # py-clob-client returns an object with asks/bids or a dict depending on version
    if hasattr(book, side):
        return list(getattr(book, side) or [])
    if isinstance(book, dict) and side in book:
        return list(book.get(side) or [])
    raise RuntimeError("Unknown order book format")


def vwap_cents_for_shares(client: ClobClient, token_id: str, shares: float) -> Optional[float]:
    """
    Computes VWAP (in cents) to buy `shares` from the ask book
    Returns None if insufficient liquidity
    """
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
        if p is None or s is None or s <= 0:
            continue
        take = min(s, need - got)
        cost += take * p
        got += take
        if got >= need - 1e-9:
            break

    if got < need - 1e-6:
        return None

    avg_price = cost / got  # dollars per share
    return avg_price * 100.0


def vwap_cents_for_usd(client: ClobClient, token_id: str, usd: float) -> Optional[float]:
    """
    Computes VWAP (in cents) for spending `usd` on the ask book
    Returns None if insufficient liquidity
    """
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
            take_shares = remaining / p
            if take_shares < 0:
                break
            shares += take_shares
            spent += take_shares * p
            break

        if spent >= budget - 1e-9:
            break

    if spent < budget - 1e-6:
        return None

    avg_price = spent / shares if shares > 0 else None
    return avg_price * 100.0 if avg_price is not None else None


def place_market_buy(
    client: ClobClient,
    token_id: str,
    usd_amount: float,
    dry_run: bool,
) -> Optional[Tuple[float, float, float, Dict[str, Any]]]:
    """
    Returns (cost_usd, shares, avg_price_cents, raw_response)
    Uses FOK market order by $ amount
    """
    amt = round(float(usd_amount), 2)
    if amt <= 0:
        return None

    if dry_run:
        # simulate "perfect" fill at midpoint buy price
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

    # typical response contains makingAmount (USDC) and takingAmount (shares) when matched
    making = safe_float(resp.get("makingAmount"))
    taking = safe_float(resp.get("takingAmount"))

    if making is None or taking is None or making <= 0 or taking <= 0:
        # fallback: query order and use size_matched if possible
        order_id = resp.get("orderID")
        if order_id:
            try:
                od = client.get_order(order_id)
                # od includes size_matched and price, but exact cost is not guaranteed here
                size_matched = safe_float(od.get("size_matched"))
                price = safe_float(od.get("price"))
                if size_matched and price:
                    cost = size_matched * price
                    avg_cents = price * 100.0
                    return (cost, size_matched, avg_cents, resp)
            except Exception:
                pass
        return None

    avg_price_cents = (making / taking) * 100.0
    return (making, taking, avg_price_cents, resp)


def init_clob_client_from_env(host: str) -> ClobClient:
    private_key = get_env("POLY_PRIVATE_KEY")
    if not private_key:
        raise SystemExit("Missing env var POLY_PRIVATE_KEY")

    funder = get_env("POLY_FUNDER")
    if not funder:
        raise SystemExit("Missing env var POLY_FUNDER")

    signature_type_s = get_env("POLY_SIGNATURE_TYPE", "1")
    try:
        signature_type = int(signature_type_s)
    except Exception:
        raise SystemExit("POLY_SIGNATURE_TYPE must be 0, 1, or 2")

    client = ClobClient(
        host,
        key=private_key,
        chain_id=POLYGON_CHAIN_ID,
        signature_type=signature_type,
        funder=funder,
    )
    client.set_api_creds(client.create_or_derive_api_creds())
    return client


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gamma_url", default=GAMMA_DEFAULT)
    ap.add_argument("--clob_host", default=CLOB_HOST_DEFAULT)

    ap.add_argument("--state_file", default=STATE_FILE_DEFAULT)

    ap.add_argument("--slug_prefix", action="append", default=["btc-updown-15m-", "btc-up-or-down-15m-"])
    ap.add_argument("--poll_seconds", type=float, default=2.0)

    # strategy params
    ap.add_argument("--chunk_stake", type=float, default=1.00)
    ap.add_argument("--trigger_below_cents", type=float, default=25.0)
    ap.add_argument("--dca_step_cents", type=float, default=2.0)
    ap.add_argument("--hedge_sum_under_cents", type=float, default=98.0)
    ap.add_argument("--signal_shares", type=float, default=10.0)

    # risk controls
    ap.add_argument("--max_stake_per_event", type=float, default=25.0)
    ap.add_argument("--min_seconds_between_orders", type=float, default=5.0)

    # execution guards
    ap.add_argument("--max_entry_vwap_cents", type=float, default=30.0)
    ap.add_argument("--max_hedge_vwap_cents", type=float, default=90.0)

    ap.add_argument("--dry_run", action="store_true")

    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)sZ %(levelname)s %(message)s",
    )
    log = logging.getLogger("poly-live-bot")

    state = load_state(args.state_file)

    client = init_clob_client_from_env(args.clob_host)

    log.info("Bot started")
    log.info(f"dry_run={args.dry_run} slug_prefix={args.slug_prefix}")

    while True:
        try:
            markets = gamma_list_markets(args.gamma_url, limit=200, offset=0, order="endDate", ascending=True)
            m = pick_current_market(markets, args.slug_prefix)

            if m is None:
                log.info("No active matching market found, sleeping")
                time.sleep(max(5.0, args.poll_seconds))
                continue

            slug = str(m.get("slug"))
            condition_id, end_date_iso, up_token, down_token = extract_up_down_tokens_from_gamma_market(m)

            # rotate state on new market
            if state.current_slug != slug:
                log.info(f"New market detected slug={slug} conditionId={condition_id}")
                state = BotState(
                    current_slug=slug,
                    current_condition_id=condition_id,
                    end_date_iso=end_date_iso,
                    up_token_id=up_token,
                    down_token_id=down_token,
                )
                save_state(args.state_file, state)

            # stop trading if hedged already, just wait for market to end then reset
            end_dt = parse_iso(state.end_date_iso) if state.end_date_iso else None
            now = utc_now()

            if end_dt is not None and now >= end_dt:
                log.info(f"Market ended slug={state.current_slug}, resetting state")
                state = BotState()
                save_state(args.state_file, state)
                time.sleep(max(2.0, args.poll_seconds))
                continue

            if not state.up_token_id or not state.down_token_id:
                log.warning("Missing token ids in state, resetting")
                state = BotState()
                save_state(args.state_file, state)
                time.sleep(max(2.0, args.poll_seconds))
                continue

            # compute signal prices
            up_px = vwap_cents_for_shares(client, state.up_token_id, args.signal_shares)
            down_px = vwap_cents_for_shares(client, state.down_token_id, args.signal_shares)

            if up_px is None or down_px is None:
                log.info("Insufficient liquidity for signal VWAP, sleeping")
                time.sleep(args.poll_seconds)
                continue

            ts = now.isoformat()

            # throttle orders
            if state.last_order_ts:
                try:
                    last = parse_iso(state.last_order_ts)
                    if (now - last).total_seconds() < args.min_seconds_between_orders:
                        time.sleep(args.poll_seconds)
                        continue
                except Exception:
                    pass

            # if hedged, do nothing
            if state.hedge is not None:
                time.sleep(args.poll_seconds)
                continue

            # entry
            if state.main is None:
                chosen_side: Optional[str] = None
                chosen_token: Optional[str] = None
                chosen_signal_px: Optional[float] = None

                both_below = (up_px < args.trigger_below_cents) and (down_px < args.trigger_below_cents)
                if both_below:
                    if up_px <= down_px:
                        chosen_side, chosen_token, chosen_signal_px = "up", state.up_token_id, up_px
                    else:
                        chosen_side, chosen_token, chosen_signal_px = "down", state.down_token_id, down_px
                elif up_px < args.trigger_below_cents:
                    chosen_side, chosen_token, chosen_signal_px = "up", state.up_token_id, up_px
                elif down_px < args.trigger_below_cents:
                    chosen_side, chosen_token, chosen_signal_px = "down", state.down_token_id, down_px

                if chosen_side is None:
                    time.sleep(args.poll_seconds)
                    continue

                # entry guard: approximate vwap for the dollars we plan to spend
                entry_vwap = vwap_cents_for_usd(client, chosen_token, args.chunk_stake)
                if entry_vwap is None or entry_vwap > args.max_entry_vwap_cents:
                    log.info(f"Entry skipped due to vwap guard side={chosen_side} vwap_cents={entry_vwap}")
                    time.sleep(args.poll_seconds)
                    continue

                fill = place_market_buy(client, chosen_token, args.chunk_stake, args.dry_run)
                if fill is None:
                    log.info(f"Entry order failed side={chosen_side}")
                    time.sleep(args.poll_seconds)
                    continue

                cost_usd, shares, avg_cents, raw = fill
                pos = Position(side=chosen_side, token_id=chosen_token)
                pos.record_fill(cost_usd=cost_usd, shares=shares, signal_price_cents=chosen_signal_px, ts=ts)
                state.main = pos
                state.last_order_ts = ts
                save_state(args.state_file, state)

                log.info(
                    f"ENTRY side={chosen_side} cost={cost_usd:.4f} shares={shares:.6f} "
                    f"avg_fill_cents={avg_cents:.3f} signal_cents={chosen_signal_px:.3f}"
                )
                time.sleep(args.poll_seconds)
                continue

            # DCA and hedge logic
            main = state.main
            assert main is not None

            # stake cap per event
            if main.total_stake_usd >= args.max_stake_per_event:
                time.sleep(args.poll_seconds)
                continue

            main_signal_px = up_px if main.side == "up" else down_px
            opp_side = "down" if main.side == "up" else "up"
            opp_token = state.down_token_id if opp_side == "down" else state.up_token_id
            opp_signal_px = down_px if opp_side == "down" else up_px

            # DCA when price <= last_add - step
            if main.last_add_price_cents is not None:
                if main_signal_px <= (main.last_add_price_cents - args.dca_step_cents):
                    # entry guard again
                    dca_vwap = vwap_cents_for_usd(client, main.token_id, args.chunk_stake)
                    if dca_vwap is not None and dca_vwap <= args.max_entry_vwap_cents:
                        fill = place_market_buy(client, main.token_id, args.chunk_stake, args.dry_run)
                        if fill is not None:
                            cost_usd, shares, avg_cents, _raw = fill
                            main.record_fill(cost_usd=cost_usd, shares=shares, signal_price_cents=main_signal_px, ts=ts)
                            state.main = main
                            state.last_order_ts = ts
                            save_state(args.state_file, state)
                            log.info(
                                f"DCA side={main.side} cost={cost_usd:.4f} shares={shares:.6f} "
                                f"avg_fill_cents={avg_cents:.3f} signal_cents={main_signal_px:.3f} "
                                f"total_stake={main.total_stake_usd:.4f} avg_entry_cents={main.avg_entry_cents():.3f}"
                            )

            # Hedge when avg_entry(main) + opp_signal < threshold
            main_avg = main.avg_entry_cents()
            if main_avg is not None:
                if (main_avg + opp_signal_px) < args.hedge_sum_under_cents:
                    # hedge guard
                    hedge_amount = main.total_stake_usd
                    hedge_vwap = vwap_cents_for_usd(client, opp_token, hedge_amount)
                    if hedge_vwap is None or hedge_vwap > args.max_hedge_vwap_cents:
                        log.info(f"HEDGE skipped due to vwap guard vwap_cents={hedge_vwap}")
                        time.sleep(args.poll_seconds)
                        continue

                    fill = place_market_buy(client, opp_token, hedge_amount, args.dry_run)
                    if fill is None:
                        log.info("HEDGE order failed")
                        time.sleep(args.poll_seconds)
                        continue

                    cost_usd, shares, avg_cents, _raw = fill
                    hedge_pos = Position(side=opp_side, token_id=opp_token)
                    hedge_pos.record_fill(cost_usd=cost_usd, shares=shares, signal_price_cents=opp_signal_px, ts=ts)
                    state.hedge = hedge_pos
                    state.hedged_ts = ts
                    state.sum_avg_at_hedge = main_avg + opp_signal_px
                    state.last_order_ts = ts
                    save_state(args.state_file, state)

                    log.info(
                        f"HEDGED opp_side={opp_side} hedge_cost={cost_usd:.4f} hedge_shares={shares:.6f} "
                        f"hedge_avg_fill_cents={avg_cents:.3f} main_avg_entry_cents={main_avg:.3f} "
                        f"sum_signal_cents={state.sum_avg_at_hedge:.3f}"
                    )

            time.sleep(args.poll_seconds)

        except KeyboardInterrupt:
            print("\nExiting on Ctrl+C")
            sys.exit(0)
        except Exception as e:
            # hard fail prevention
            logging.getLogger("poly-live-bot").exception(f"Loop error: {e}")
            time.sleep(3.0)


if __name__ == "__main__":
    main()

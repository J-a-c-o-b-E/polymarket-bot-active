# src/polymarket_bot/main.py
from __future__ import annotations

import argparse
import logging
import os
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import List, Optional

from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds

from polymarket_bot.gamma import (
    gamma_list_events,
    pick_current_event,
    pick_active_market_from_event,
    extract_up_down_tokens_from_gamma_market,
    parse_iso,
)
from polymarket_bot.execution import (
    vwap_cents_for_shares,
    vwap_cents_for_usd,
    place_market_buy,
)
from polymarket_bot.state import BotState, Position, load_state, save_state
from polymarket_bot.strategy import (
    StrategyParams,
    choose_entry_side,
    should_dca,
    should_hedge,
)

POLYGON_CHAIN_ID = 137


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def get_env(name: str, default: Optional[str] = None) -> Optional[str]:
    v = os.environ.get(name)
    return v if v is not None and v != "" else default


def env_truthy(name: str, default: str = "0") -> bool:
    v = (get_env(name, default) or "").strip().lower()
    return v in ("1", "true", "yes", "y", "on")


def parse_env_slug_prefixes(defaults: List[str]) -> List[str]:
    out: List[str] = []

    one = get_env("SLUG_PREFIX")
    if one:
        out.append(one.strip())

    many = get_env("SLUG_PREFIXES")
    if many:
        parts = [p.strip() for p in many.split(",")]
        out.extend([p for p in parts if p])

    out = [p for p in out if p]
    return out if out else defaults


def init_clob_client_from_env(host: str) -> ClobClient:
    private_key = get_env("POLY_PRIVATE_KEY")
    funder = get_env("POLY_FUNDER")
    signature_type_s = get_env("POLY_SIGNATURE_TYPE", "1")

    if not private_key:
        raise SystemExit("missing env var POLY_PRIVATE_KEY")
    if not funder:
        raise SystemExit("missing env var POLY_FUNDER")

    try:
        signature_type = int(signature_type_s)
    except Exception:
        raise SystemExit("POLY_SIGNATURE_TYPE must be an int")

    client = ClobClient(
        host,
        key=private_key,
        chain_id=POLYGON_CHAIN_ID,
        signature_type=signature_type,
        funder=funder,
    )

    try:
        api_creds = ApiCreds(
            api_key=os.environ["POLY_API_KEY"],
            api_secret=os.environ["POLY_API_SECRET"],
            api_passphrase=os.environ["POLY_API_PASSPHRASE"],
        )
    except KeyError as e:
        raise SystemExit(
            f"missing env var {e.args[0]} (expected POLY_API_KEY / POLY_API_SECRET / POLY_API_PASSPHRASE)"
        )

    client.set_api_creds(api_creds)
    return client


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path in ("/", "/health", "/healthz"):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *_args) -> None:
        return  # silence


def run_bot_loop(
    args: argparse.Namespace,
    slug_prefixes: List[str],
    log: logging.Logger,
    params: StrategyParams,
    state_file: str,
    client: ClobClient,
) -> None:
    state = load_state(state_file)

    while True:
        try:
            # discovery via gamma, but tolerate lag by continuing with saved state
            events = gamma_list_events(args.gamma_url, limit=200, offset=0, closed=False)
            ev = pick_current_event(events, slug_prefixes)

            if ev is None:
                if state.up_token_id and state.down_token_id and state.end_date_iso:
                    end_dt = parse_iso(state.end_date_iso)
                    if utc_now() < end_dt:
                        log.info("gamma missed event, continuing with current state")
                        # no continue: keep using state below
                    else:
                        log.info("state market ended, resetting")
                        state = BotState()
                        save_state(state_file, state)
                        time.sleep(max(2.0, args.poll_seconds))
                        continue
                else:
                    log.info("no active matching market found")
                    time.sleep(max(5.0, args.poll_seconds))
                    continue
            else:
                market = pick_active_market_from_event(ev)
                if market is None:
                    log.info("matched event but it has no markets")
                    time.sleep(max(5.0, args.poll_seconds))
                    continue

                slug = str(market.get("slug") or ev.get("slug") or "")
                condition_id, end_date_iso, up_token, down_token = extract_up_down_tokens_from_gamma_market(market)

                if state.current_slug != slug:
                    log.info(f"new market detected slug={slug}")
                    state = BotState(
                        current_slug=slug,
                        current_condition_id=condition_id,
                        end_date_iso=end_date_iso,
                        up_token_id=up_token,
                        down_token_id=down_token,
                    )
                    save_state(state_file, state)

            now = utc_now()
            ts = now.isoformat()

            if not state.up_token_id or not state.down_token_id or not state.end_date_iso:
                log.info("state missing token ids or end date, waiting")
                time.sleep(max(2.0, args.poll_seconds))
                continue

            end_dt = parse_iso(state.end_date_iso)
            if now >= end_dt:
                log.info("market ended resetting state")
                state = BotState()
                save_state(state_file, state)
                time.sleep(max(2.0, args.poll_seconds))
                continue

            if state.last_order_ts:
                try:
                    last = parse_iso(state.last_order_ts)
                    if (now - last).total_seconds() < args.min_seconds_between_orders:
                        time.sleep(args.poll_seconds)
                        continue
                except Exception:
                    pass

            up_px = vwap_cents_for_shares(client, state.up_token_id, args.signal_shares)
            down_px = vwap_cents_for_shares(client, state.down_token_id, args.signal_shares)
            if up_px is None or down_px is None:
                time.sleep(args.poll_seconds)
                continue

            # if already hedged for this event, do nothing
            if state.hedge is not None:
                time.sleep(args.poll_seconds)
                continue

            # entry
            if state.main is None:
                entry_side = choose_entry_side(up_px, down_px, params.trigger_below_cents)
                if entry_side is None:
                    time.sleep(args.poll_seconds)
                    continue

                token_id = state.up_token_id if entry_side == "up" else state.down_token_id
                signal_px = up_px if entry_side == "up" else down_px

                entry_vwap = vwap_cents_for_usd(client, token_id, params.chunk_stake)
                if entry_vwap is None or entry_vwap > args.max_entry_vwap_cents:
                    time.sleep(args.poll_seconds)
                    continue

                fill = place_market_buy(client, token_id, params.chunk_stake, args.dry_run)
                if fill is None:
                    time.sleep(args.poll_seconds)
                    continue

                cost_usd, shares, avg_cents, _raw = fill
                pos = Position(side=entry_side, token_id=token_id)
                pos.record_fill(cost_usd=cost_usd, shares=shares, signal_price_cents=signal_px, ts=ts)
                state.main = pos
                state.last_order_ts = ts
                save_state(state_file, state)

                log.info(
                    f"entry side={entry_side} cost={cost_usd:.4f} shares={shares:.6f} "
                    f"avg_fill_cents={avg_cents:.3f} signal_cents={signal_px:.3f}"
                )
                time.sleep(args.poll_seconds)
                continue

            main = state.main
            if main is None:
                time.sleep(args.poll_seconds)
                continue

            if main.total_stake_usd >= params.max_stake_per_event:
                time.sleep(args.poll_seconds)
                continue

            main_signal_px = up_px if main.side == "up" else down_px
            opp_side = "down" if main.side == "up" else "up"
            opp_token = state.down_token_id if opp_side == "down" else state.up_token_id
            opp_signal_px = down_px if opp_side == "down" else up_px

            # dca
            if should_dca(main, main_signal_px, params.dca_step_cents):
                dca_vwap = vwap_cents_for_usd(client, main.token_id, params.chunk_stake)
                if dca_vwap is not None and dca_vwap <= args.max_entry_vwap_cents:
                    fill = place_market_buy(client, main.token_id, params.chunk_stake, args.dry_run)
                    if fill is not None:
                        cost_usd, shares, avg_cents, _raw = fill
                        main.record_fill(cost_usd=cost_usd, shares=shares, signal_price_cents=main_signal_px, ts=ts)
                        state.main = main
                        state.last_order_ts = ts
                        save_state(state_file, state)
                        log.info(
                            f"dca side={main.side} cost={cost_usd:.4f} shares={shares:.6f} "
                            f"avg_fill_cents={avg_cents:.3f} signal_cents={main_signal_px:.3f} "
                            f"total_stake={main.total_stake_usd:.4f}"
                        )

            # hedge
            hedge_ok, sum_signal = should_hedge(main, opp_signal_px, params.hedge_sum_under_cents)
            if hedge_ok:
                hedge_amount = main.total_stake_usd
                hedge_vwap = vwap_cents_for_usd(client, opp_token, hedge_amount)
                if hedge_vwap is None or hedge_vwap > args.max_hedge_vwap_cents:
                    time.sleep(args.poll_seconds)
                    continue

                fill = place_market_buy(client, opp_token, hedge_amount, args.dry_run)
                if fill is None:
                    time.sleep(args.poll_seconds)
                    continue

                cost_usd, shares, avg_cents, _raw = fill
                hedge_pos = Position(side=opp_side, token_id=opp_token)
                hedge_pos.record_fill(cost_usd=cost_usd, shares=shares, signal_price_cents=opp_signal_px, ts=ts)
                state.hedge = hedge_pos
                state.hedged_ts = ts
                state.sum_avg_at_hedge = float(sum_signal) if sum_signal is not None else None
                state.last_order_ts = ts
                save_state(state_file, state)

                log.info(
                    f"hedged opp_side={opp_side} hedge_cost={cost_usd:.4f} hedge_shares={shares:.6f} "
                    f"hedge_avg_fill_cents={avg_cents:.3f} sum_signal_cents={state.sum_avg_at_hedge}"
                )

            time.sleep(args.poll_seconds)

        except KeyboardInterrupt:
            return
        except Exception as e:
            log.exception(f"loop error {e}")
            time.sleep(3.0)


def main() -> None:
    load_dotenv()

    ap = argparse.ArgumentParser()
    ap.add_argument("--gamma_url", default=get_env("GAMMA_URL", "https://gamma-api.polymarket.com"))
    ap.add_argument("--clob_host", default=get_env("CLOB_HOST", "https://clob.polymarket.com"))
    ap.add_argument("--state_file", default=get_env("STATE_FILE", "state/live_bot_state.json"))

    ap.add_argument("--slug_prefix", action="append", default=[])
    ap.add_argument("--poll_seconds", type=float, default=float(get_env("POLL_SECONDS", "2.0")))

    ap.add_argument("--chunk_stake", type=float, default=float(get_env("CHUNK_STAKE", "1.0")))
    ap.add_argument("--trigger_below_cents", type=float, default=float(get_env("TRIGGER_BELOW_CENTS", "25.0")))
    ap.add_argument("--dca_step_cents", type=float, default=float(get_env("DCA_STEP_CENTS", "2.0")))
    ap.add_argument("--hedge_sum_under_cents", type=float, default=float(get_env("HEDGE_SUM_UNDER_CENTS", "98.0")))
    ap.add_argument("--signal_shares", type=float, default=float(get_env("SIGNAL_SHARES", "10.0")))

    ap.add_argument("--max_stake_per_event", type=float, default=float(get_env("MAX_STAKE_PER_EVENT", "25.0")))
    ap.add_argument("--min_seconds_between_orders", type=float, default=float(get_env("MIN_SECONDS_BETWEEN_ORDERS", "5.0")))

    ap.add_argument("--max_entry_vwap_cents", type=float, default=float(get_env("MAX_ENTRY_VWAP_CENTS", "30.0")))
    ap.add_argument("--max_hedge_vwap_cents", type=float, default=float(get_env("MAX_HEDGE_VWAP_CENTS", "90.0")))

    ap.add_argument("--dry_run", action="store_true")
    args = ap.parse_args()

    # allow DRY_RUN=1 in env
    if env_truthy("DRY_RUN", "0"):
        args.dry_run = True

    defaults = ["btc-updown-15m-", "btc-up-or-down-15m-"]
    slug_prefixes: List[str] = args.slug_prefix if args.slug_prefix else parse_env_slug_prefixes(defaults)

    logging.basicConfig(
        level=getattr(logging, get_env("LOG_LEVEL", "INFO"), logging.INFO),
        format="%(asctime)sZ %(levelname)s %(message)s",
    )

    # suppress noisy transport logs (some builds still emit; this forces it off)
    logging.getLogger("httpx").disabled = True
    logging.getLogger("httpcore").disabled = True

    log = logging.getLogger("polymarket-bot")

    params = StrategyParams(
        chunk_stake=args.chunk_stake,
        trigger_below_cents=args.trigger_below_cents,
        dca_step_cents=args.dca_step_cents,
        hedge_sum_under_cents=args.hedge_sum_under_cents,
        max_stake_per_event=args.max_stake_per_event,
    )

    client = init_clob_client_from_env(args.clob_host)

    log.info("bot started")
    log.info(f"dry_run={args.dry_run}")
    log.info(f"gamma_url={args.gamma_url} clob_host={args.clob_host}")
    log.info(f"slug_prefixes={slug_prefixes}")
    log.info(
        f"params chunk_stake={params.chunk_stake} trigger_below_cents={params.trigger_below_cents} "
        f"dca_step_cents={params.dca_step_cents} hedge_sum_under_cents={params.hedge_sum_under_cents} "
        f"max_stake_per_event={params.max_stake_per_event}"
    )

    # bot loop in background thread
    bot_thread = threading.Thread(
        target=run_bot_loop,
        args=(args, slug_prefixes, log, params, args.state_file, client),
        daemon=False,
        name="bot-loop",
    )
    bot_thread.start()

    # health server in foreground so railway sees a web process
    port = int(os.environ.get("PORT", "8080"))
    log.info(f"health server listening on 0.0.0.0:{port}")
    HTTPServer(("0.0.0.0", port), HealthHandler).serve_forever()


if __name__ == "__main__":
    main()

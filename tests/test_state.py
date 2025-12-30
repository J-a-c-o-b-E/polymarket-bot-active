from polymarket_bot.state import BotState, Position


def test_state_roundtrip() -> None:
    st = BotState(
        current_slug="x",
        current_condition_id="c",
        end_date_iso="2025-01-01T00:00:00+00:00",
        up_token_id="1",
        down_token_id="2",
        main=Position(side="up", token_id="1", total_stake_usd=3.0, total_shares=10.0, last_add_price_cents=30.0),
    )
    d = st.to_json()
    st2 = BotState.from_json(d)
    assert st2.current_slug == "x"
    assert st2.main is not None
    assert st2.main.side == "up"
    assert abs(st2.main.total_stake_usd - 3.0) < 1e-9

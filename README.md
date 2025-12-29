# polymarket live bot

this repo runs a 24 7 python bot that trades the current btc up down 15m market on polymarket clob

## what it does
polls the active btc up down 15m market from gamma api
reads live order books from clob
enters when one side vwap is below a trigger
dca adds when price drops by a step
hedges with the opposite side when main avg entry plus opposite signal price is under a threshold
persists state to a local json file so restarts do not forget positions

## requirements
python 3 10 plus
a machine that stays online
polymarket clob api creds derived from your wallet

## setup
copy config.example.env to .env
fill in the variables
install dependencies
pip install -r requirements.txt

## dry run
scripts/run_dry.sh

## live
scripts/run_live.sh

## state and logs
state is saved to state/live_bot_state.json
logs are written to stdout and optionally to logs/bot.log if you use docker compose volume mounts

## environment variables
POLY_PRIVATE_KEY
POLY_FUNDER
POLY_SIGNATURE_TYPE

optional
GAMMA_URL
CLOB_HOST
STATE_FILE
LOG_LEVEL

## safety notes
this code does not guarantee profitable trading
slippage and partial fills can break backtest assumptions
start with very small chunk stake and a low max stake per event

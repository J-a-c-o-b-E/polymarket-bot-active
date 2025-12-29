from python:3.11-slim

env pythonunbuffered=1 \
    pip_no_cache_dir=1

workdir /app

copy requirements.txt /app/requirements.txt
run pip install -r /app/requirements.txt

copy src /app/src
copy scripts /app/scripts

run mkdir -p /app/state /app/logs

cmd ["python", "-m", "polymarket_bot.main"]

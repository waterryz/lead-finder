#!/bin/sh
# Entry point: web dashboard (always) + Telegram bot (optional, set RUN_BOT=1).
# Both share the same SQLite file on the mounted /data volume.
set -e

if [ "$RUN_BOT" = "1" ] && [ -n "$TELEGRAM_BOT_TOKEN" ]; then
  echo "[start] launching telegram bot"
  python bot.py &
fi

echo "[start] launching web dashboard on port ${PORT:-8000}"
exec uvicorn webapp:app --host 0.0.0.0 --port "${PORT:-8000}"

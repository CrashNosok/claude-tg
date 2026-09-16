#!/usr/bin/env sh
# Usage: ./run.sh [start|stop|restart|status|logs]   (default: restart)
# Bot runs in its own tmux session "claude-tg-bot"; logs go to bot.log (gitignored).
cd "$(dirname "$0")"
BOT=claude-tg-bot

stop()   { tmux kill-session -t "$BOT" 2>/dev/null && echo "stopped" || echo "not running"; }
start()  { tmux new -d -s "$BOT" "uv run --env-file .env bot.py 2>&1 | tee -a bot.log"; echo "started (session $BOT)"; }
status() { tmux has-session -t "$BOT" 2>/dev/null && echo "running" || echo "not running"; }

case "${1:-restart}" in
  start)   start ;;
  stop)    stop ;;
  restart) stop; start; sleep 2; tail -n 20 bot.log ;;
  status)  status ;;
  logs)    tail -f bot.log ;;
  *)       echo "usage: $0 [start|stop|restart|status|logs]"; exit 1 ;;
esac

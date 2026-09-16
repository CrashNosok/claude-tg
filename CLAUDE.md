# claude-tg

Telegram bot that controls a Claude Code session in tmux. **Public repository.**

## Secrets and PII — hard rules
- Secrets only via env vars (`TG_BOT_TOKEN`, ...). `.env` is gitignored; `.env.example` holds empty values only.
- Never hardcode or paste into code, docs, commits, or examples: bot tokens, Telegram user ids/usernames/names, host paths, machine names. Use obvious placeholders (`123456789`).
- Never log message text or terminal screen content — the terminal may show keys. Log only user ids for denied access, at WARNING.
- Never commit terminal screenshots or `capture-pane` dumps.
- Bot answers only allowlisted users in private chats and stays silent to everyone else.
- Before committing: `git diff --cached | grep -iE 'token|api[_-]?key|[0-9]{9,}'` and read the hits.

## Stack
Python 3.12, `uv`, `python-telegram-bot`, tmux. One file: `bot.py`. Test: `uv run python test_bot.py`.

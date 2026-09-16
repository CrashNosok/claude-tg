# claude-tg

Control a Claude Code session from Telegram. Claude runs in tmux; the bot sends keys and pushes the screen when it changes.

```sh
uv sync
cp .env.example .env   # fill TG_BOT_TOKEN, TG_ALLOWED_USER_IDS
tmux new -d -s claude -c ~/projects/myapp claude   # or set TMUX_DIR in .env and let the bot create it
uv run --env-file .env bot.py
```

Full model replies and tool calls are streamed from the Claude Code transcript (`~/.claude/projects/<cwd>/*.jsonl`). Screen snapshots are still pushed when the screen settles (`AUTO_SCREEN=0` to disable). Send `/start` first — the bot only pushes to chats that opened it. Commands: `/start` `/screen` `/esc` `/c` (Ctrl+C) `/enter` `/tab` `/up` `/down` `/y` `/key <tmux-key>` (e.g. `/key C-d`). Any other text is typed into the session + Enter.

Claude slash commands (`/context`, `/compact`, `/clear`, ...) are not bot commands, so they are typed into the session as-is.

When Claude finishes a task (the "esc to interrupt" hint disappears), the bot sends `✅ Done: <prompt>` with a `claude-log-<timestamp>.md` file: the prompt, every reply, tool calls and truncated tool results since that prompt.

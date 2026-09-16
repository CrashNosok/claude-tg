"""Telegram <-> tmux bridge for a Claude Code session."""
import glob
import html
import json
import logging
import os
import re
import subprocess
import sys
import time
from datetime import datetime

from telegram import Update
from telegram.ext import AIORateLimiter, Application, CommandHandler, ContextTypes, MessageHandler, filters

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
log = logging.getLogger("claude-tg")

TOKEN = os.environ.get("TG_BOT_TOKEN", "")
ALLOWED = [int(x) for x in os.environ.get("TG_ALLOWED_USER_IDS", "").split(",") if x.strip()]
SESSION = os.environ.get("TMUX_SESSION", "claude")
TARGET = "=" + SESSION  # exact session match ("-t claude" prefix-matches "claude-tg-bot")
PANE = TARGET + ":"  # pane/window commands need the trailing colon
TMUX_CMD = os.environ.get("TMUX_CMD", "claude")
TMUX_DIR = os.environ.get("TMUX_DIR", os.getcwd())
AUTO_SCREEN = os.environ.get("AUTO_SCREEN", "1") == "1"
WORKING_MARK = "esc to interrupt"  # screen fallback when ~/.claude/sessions/<pid>.json is unavailable
DONE_TICKS = 3                     # idle + screen unchanged this many ticks in a row => task finished
ROWS = int(os.environ.get("TMUX_ROWS", "300"))  # Claude Code redraws in place (no scrollback): a tall pane is our buffer
ANCHOR = 20                        # last sent lines we locate on the next capture to find what is new

TG_MAX = 4000  # Telegram hard limit is 4096; leave room for <pre> tags
MIN_PUSH = int(os.environ.get("MIN_PUSH_SEC", "3"))  # flush the outgoing queue at most this often
ECHO_SEC = 2  # after user input: if the stream sent nothing within this time, send the screen once (feedback for /clear, /esc, ...)
KEYS = {
    "esc": "Escape", "c": "C-c", "enter": "Enter", "tab": "Tab",
    "up": "Up", "down": "Down", "left": "Left", "right": "Right",
}


# --- tmux ---------------------------------------------------------------
def tmux(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["tmux", *args], capture_output=True, text=True)


def ensure_session() -> bool:
    """A shell session with Claude started inside it, so /exit drops to the shell (cd, restart claude) instead of killing the pane."""
    if tmux("has-session", "-t", TARGET).returncode == 0:
        return False
    tmux("new-session", "-d", "-s", SESSION, "-c", TMUX_DIR, "-x", "80", "-y", str(ROWS))
    send(TMUX_CMD, literal=True)
    send("Enter")
    return True


def send(*keys: str, literal: bool = False) -> None:
    tmux("send-keys", "-t", PANE, *(["-l"] if literal else []), *keys)


def screen() -> str:
    return tmux("capture-pane", "-p", "-t", PANE).stdout.rstrip()


def keep_tall() -> None:
    """Hold the pane at ROWS lines so long outputs stay capturable; an attached client keeps its own size."""
    attached, height = tmux("display", "-p", "-t", PANE, "#{session_attached} #{pane_height}").stdout.split() or ("1", "0")
    if attached == "0" and height != str(ROWS):
        tmux("resize-window", "-t", PANE, "-y", str(ROWS))


def pane_pid() -> str:
    return tmux("display", "-p", "-t", PANE, "#{pane_pid}").stdout.strip()


def session_info() -> dict:
    """Claude Code publishes status/version/sessionId/cwd in ~/.claude/sessions/<pid>.json; {} when no Claude runs in the pane.

    The pane runs a shell, so Claude is a child (or grandchild, via a wrapper) of the pane pid.
    """
    pids, todo = [], [pane_pid()]
    while todo and len(pids) < 20:
        pid = todo.pop()
        pids.append(pid)
        todo += subprocess.run(["pgrep", "-P", pid], capture_output=True, text=True).stdout.split()
    for pid in pids:
        try:
            with open(os.path.expanduser(f"~/.claude/sessions/{pid}.json")) as f:
                return json.load(f)
        except (OSError, ValueError):
            continue
    return {}


def is_busy(info: dict, scr: str) -> bool:
    return info["status"] == "busy" if "status" in info else WORKING_MARK in scr


def pane_cwd(info: dict) -> str:
    return info.get("cwd") or tmux("display", "-p", "-t", PANE, "#{pane_current_path}").stdout.strip() or TMUX_DIR


SPINNER = re.compile(r"^\s*(\S\s+\w+…[^\n]*)$", re.M)


def header(info: dict, raw: str, busy: bool, since: float) -> str:
    """One line above every message: Claude Code session (version, project, state) or plain shell (cwd)."""
    cwd = pane_cwd(info)
    if not info:
        return f"🐚 <b>shell</b> · <code>{html.escape(cwd.replace(os.path.expanduser('~'), '~'))}</code>"
    project = os.path.basename(cwd)
    if busy:
        m = SPINNER.search(raw)
        state = "⏳ " + (m.group(1).strip() if m else f"working {time.time() - since:.0f}s")
    else:
        state = "✅ idle"
    return f"<b>Claude Code</b> {info.get('version', '')} · <code>{html.escape(project)}</code> · {html.escape(state)}"


# --- pure helpers (tested) ----------------------------------------------
NOISE = re.compile(r"[▐▝▜▛█▀]|^\s*[─━═]+\s*$|^\s*⏵⏵")  # logo banner, separators, status bar
VOLATILE = re.compile(r"^\s*(❯|\S\s+\w+….*)?\s*$")   # empty input box, spinner ("✽ Kneading…"), blank


def clean_lines(text: str) -> list[str]:
    out: list[str] = []
    for l in text.splitlines():
        if NOISE.search(l):
            continue
        l = l.replace("\xa0", " ").rstrip()  # the input box renders typed text with nbsp
        if l or (out and out[-1]):  # collapse blank runs
            out.append(l)
    return out


def squeeze(lines: list[str]) -> str:
    return "\n".join(lines).strip("\n")


def clean(text: str) -> str:
    """Strip terminal chrome so the screen reads well in Telegram."""
    return squeeze(clean_lines(text))


def card(head: str, text: str) -> str:
    return f"{head}\n<pre>{html.escape(text)}</pre>"


def chunks(text: str) -> list[str]:
    return [text[i:i + TG_MAX] for i in range(0, len(text), TG_MAX)] or [text]


def find_block(hay: list[str], block: list[str]) -> int:
    """Index just past the last occurrence of `block` in `hay`, or -1."""
    n = len(block)
    for i in range(len(hay) - n, -1, -1):
        if hay[i:i + n] == block:
            return i + n
    return -1


def drop_echo(lines: list[str], typed: list[str]) -> list[str]:
    """Hide the input-box / conversation echo of text the user just typed through the bot."""
    echoes = {"❯ " + t.strip().splitlines()[0] for t in typed if t.strip()}
    return [l for l in lines if l.strip() not in echoes]


def settled(lines: list[str]) -> list[str]:
    """Drop the still-changing tail (spinner, empty input box / bare shell prompt)."""
    while lines and VOLATILE.match(lines[-1]):
        lines.pop()
    return lines


def new_lines(prev: list[str], cur: list[str], anchor: list[str], busy: bool) -> tuple[list[str], list[str]]:
    """Lines of `cur` after the already-sent `anchor` that have stopped changing. Returns (to send, new anchor).

    The screen is a moving window (Claude Code redraws in place, so tmux keeps no scrollback): the anchor - the
    last ANCHOR sent lines - is located on the new capture and everything after it is new. A shorter anchor prefix
    or suffix handles a sent line rewritten in place / scrolled off ("Running…" -> output). Held back until the next tick: the spinner,
    the empty input box, while busy the last text line (may still be growing), and anything not yet present in
    the previous capture (i.e. seen only once).
    """
    p = -1  # newest piece of the anchor still on screen: its head may have scrolled off, its tail been rewritten
    for end in range(len(anchor), 0, -1):
        for m in range(end, 0, -1):
            if any(anchor[end - m:end]) and (p := find_block(cur, anchor[end - m:end])) >= 0:
                break
        if p >= 0:
            break
    if p < 0:
        p = 0  # ponytail: anchor gone (screen cleared, or a burst longer than ROWS in one tick) -> resend the screen
    cand = settled(cur[p:])
    if busy and cand:
        cand.pop()
    while cand and find_block(prev, cand) < 0:
        cand.pop()
    if not cand:
        return [], anchor
    return cand, (anchor + cand)[-ANCHOR:]


def user_prompt(o: dict) -> str | None:
    """Text of a human prompt entry (None for tool_result entries and non-user types)."""
    if o.get("type") != "user" or o.get("isSidechain"):
        return None
    c = o.get("message", {}).get("content", "")
    if isinstance(c, str):
        return c or None
    texts = [b.get("text", "") for b in c if b.get("type") == "text"]
    return "\n".join(texts) or None


def task_log(task: dict) -> str:
    """Markdown log of one task: prompt, assistant text, tool calls and (truncated) tool results."""
    lines = [f"# Claude task — {task['started']}", "", "## Prompt", "", task["prompt"], "", "## Log", ""]
    for o in task["entries"]:
        c = o.get("message", {}).get("content", [])
        if isinstance(c, str):
            c = [{"type": "text", "text": c}]
        for b in c:
            t = b.get("type")
            if t == "text" and o["type"] == "assistant":
                lines += ["**Claude:**", "", b["text"], ""]
            elif t == "tool_use":
                lines += [f"🔧 **{b['name']}**", "```json", json.dumps(b.get("input", {}), ensure_ascii=False, indent=1)[:2000], "```", ""]
            elif t == "tool_result":
                r = b.get("content", "")
                r = r if isinstance(r, str) else "\n".join(x.get("text", "") for x in r if isinstance(x, dict))
                lines += ["```", r[:1500] + ("\n…" if len(r) > 1500 else ""), "```", ""]
    return "\n".join(lines)


# --- transcript tail ----------------------------------------------------
def transcript_dir(cwd: str) -> str:
    """Claude Code keeps transcripts under ~/.claude/projects/<cwd with non-alnum -> '-'>."""
    return os.path.expanduser("~/.claude/projects/") + re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(cwd))


def newest_transcript(info: dict) -> str | None:
    """Transcript of the Claude running in our pane (it registers pid -> sessionId/cwd); newest file in the pane's cwd as fallback."""
    cwd = pane_cwd(info)
    if "sessionId" in info:
        path = os.path.join(transcript_dir(cwd), info["sessionId"] + ".jsonl")
        if os.path.exists(path):
            return path
    files = glob.glob(os.path.join(transcript_dir(cwd), "*.jsonl"))
    return max(files, key=os.path.getmtime) if files else None


def read_new(t: dict, info: dict) -> list[str]:
    """Return new complete lines since last call; follows the newest session file."""
    path = newest_transcript(info)
    if not path:
        return []
    if path != t.get("file"):
        # first attach = skip history; a brand-new session file = read from start
        t["file"], t["pos"] = path, (os.path.getsize(path) if t.get("file") is None else 0)
    with open(path, "rb") as f:
        f.seek(t["pos"])
        data = f.read()
    complete = data.rfind(b"\n") + 1
    t["pos"] += complete
    return data[:complete].decode("utf-8", "replace").splitlines()


# --- handlers -----------------------------------------------------------
async def reply_screen(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    raw, info = screen(), session_info()
    await update.message.reply_html(card(header(info, raw, is_busy(info, raw), ctx.bot_data["state"].get("since", 0)), clean(raw)[-TG_MAX:] or "(empty)"))


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    ensure_session()
    await reply_screen(update, ctx)


async def remember_chat(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs before every allowed handler: any message (re)subscribes the chat to pushes, so a bot restart needs no /start."""
    ctx.bot_data.setdefault("chats", set()).add(update.effective_chat.id)


async def cmd_key(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    name = update.message.text.lstrip("/").split("@")[0].split()[0]
    key = KEYS.get(name) or (ctx.args[0] if ctx.args else None)
    if key:
        mark_input(ctx)
        send(key)


def mark_input(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    ctx.bot_data["state"]["input_at"] = time.time()


def type_in(ctx: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    ctx.bot_data["typed"] = (ctx.bot_data.get("typed", []) + [text])[-10:]  # remembered to hide the echo
    mark_input(ctx)
    send(text, literal=True)
    send("Enter")


async def cmd_yes(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    type_in(ctx, "y")


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    type_in(ctx, update.message.text)


async def on_denied(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    # Silent: never reveal the bot to strangers, never log their text.
    log.warning("denied user_id=%s", update.effective_user.id if update.effective_user else "?")


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    d, now, info = ctx.bot_data["state"], time.time(), session_info()
    alive = tmux("has-session", "-t", TARGET).returncode == 0
    claude = ("busy" if is_busy(info, screen()) else "idle") if info else "not running (shell)"
    await update.message.reply_text(
        f"session: {'alive' if alive else 'DEAD'}\n"
        f"claude: {claude}\n"
        f"transcript: {os.path.basename(ctx.bot_data['tail'].get('file') or '') or 'NOT FOUND'}\n"
        f"screen unchanged for: {now - d['last_change']:.0f}s\n"
        f"queued lines: {len(d['queue'])}, last push: {now - d['last_push']:.0f}s ago"
    )


async def on_error(_: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log.warning("telegram error: %s: %s", type(ctx.error).__name__, ctx.error)


async def push(ctx: ContextTypes.DEFAULT_TYPE, text: str, **kw) -> bool:
    ok = True
    for chat_id in ctx.bot_data.get("chats", ()):
        try:
            await ctx.bot.send_message(chat_id, text, **kw)
        except Exception as e:  # keep the loop alive; no message text in logs
            log.warning("push failed chat_id=%s: %s", chat_id, type(e).__name__)
            ok = False
    return ok


async def flush(ctx: ContextTypes.DEFAULT_TYPE, head: str) -> None:
    """Send queued screen lines as one <pre> block (split at TG_MAX); keep them queued if sending fails."""
    d = ctx.bot_data["state"]
    text = squeeze(d["queue"])
    if not text:
        d["queue"].clear()
        return
    for part in chunks(text):
        if not await push(ctx, card(head, part), parse_mode="HTML"):
            return
    d["queue"].clear()
    d["last_push"] = time.time()


async def finish_task(ctx: ContextTypes.DEFAULT_TYPE, task: dict) -> None:
    name = f"claude-log-{datetime.now():%Y%m%d-%H%M%S}.md"
    caption = "✅ Done: " + task["prompt"][:200]
    for chat_id in ctx.bot_data.get("chats", ()):
        try:
            await ctx.bot.send_document(chat_id, task_log(task).encode(), filename=name, caption=caption)
        except Exception as e:
            log.warning("log push failed chat_id=%s: %s", chat_id, type(e).__name__)


async def tick(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    d, now = ctx.bot_data["state"], time.time()
    task = ctx.bot_data["task"]
    if ensure_session():
        log.info("tmux session recreated")
    info = session_info()
    shell = not info
    for line in read_new(ctx.bot_data["tail"], info):
        try:
            o = json.loads(line)
        except ValueError:
            continue
        if prompt := user_prompt(o):
            task.clear()
            task.update(prompt=prompt, started=f"{datetime.now():%Y-%m-%d %H:%M}", entries=[])
        elif task and o.get("type") in ("user", "assistant") and not o.get("isSidechain"):
            task["entries"].append(o)

    keep_tall()
    raw = screen()
    cur = clean_lines(raw)
    busy = is_busy(info, raw)
    fresh, d["anchor"] = new_lines(d["prev"], cur, d["anchor"], busy)
    if AUTO_SCREEN:
        d["queue"] += fresh if shell else drop_echo(fresh, ctx.bot_data.get("typed", []))  # a shell shows the typed command
    changed = cur != d["prev"]
    if changed:
        d["last_change"] = now
    d["prev"] = cur
    if busy and not d["working"]:
        d["since"] = now
    head = header(info, raw, busy, d.get("since", now))
    if d["queue"] and now - d["last_push"] >= MIN_PUSH:
        await flush(ctx, head)
    if d.get("input_at"):
        if d["last_push"] > d["input_at"] or d["queue"]:
            d["input_at"] = 0  # the stream is answering this input
        elif now - d["input_at"] >= ECHO_SEC:
            d["input_at"] = 0
            if await push(ctx, card(head, clean(raw)[-TG_MAX:] or "(empty)"), parse_mode="HTML"):
                d["anchor"] = settled(list(cur))[-ANCHOR:]  # the snapshot showed everything: do not stream it again

    d["idle_ticks"] = 0 if busy or changed else d["idle_ticks"] + 1
    if busy:
        d["working"] = True
    elif d["working"] and d["idle_ticks"] >= DONE_TICKS and not d["queue"]:
        # Screen idle and everything delivered: the log file is the last message of the task.
        d["working"] = False
        if task:
            await finish_task(ctx, task)
            task.clear()


async def set_menu(app: Application) -> None:
    await app.bot.set_my_commands([
        ("screen", "show terminal"), ("status", "alive? busy? queue?"), ("esc", "Escape"), ("c", "Ctrl+C"), ("enter", "Enter"),
        ("tab", "Tab"), ("up", "Up"), ("down", "Down"), ("y", "y + Enter"),
        ("key", "any tmux key, e.g. /key C-d"),
    ])


def main() -> None:
    if not TOKEN or not ALLOWED:
        sys.exit("TG_BOT_TOKEN and TG_ALLOWED_USER_IDS must be set")
    ensure_session()
    app = (
        Application.builder().token(TOKEN)
        .connect_timeout(30).read_timeout(30).write_timeout(30).pool_timeout(30)
        .rate_limiter(AIORateLimiter()).post_init(set_menu).build()
    )
    app.add_error_handler(on_error)
    ok = filters.User(user_id=ALLOWED) & filters.ChatType.PRIVATE
    app.add_handler(MessageHandler(ok, remember_chat), group=-1)
    app.add_handler(CommandHandler("start", cmd_start, ok))
    app.add_handler(CommandHandler("screen", reply_screen, ok))
    app.add_handler(CommandHandler("status", cmd_status, ok))
    app.add_handler(CommandHandler("y", cmd_yes, ok))
    app.add_handler(CommandHandler([*KEYS, "key"], cmd_key, ok))
    # Anything else (incl. unknown /commands like /context) is typed into Claude verbatim.
    app.add_handler(MessageHandler(filters.TEXT & ok, on_text))
    app.add_handler(MessageHandler(~ok, on_denied))
    now = time.time()
    start = clean_lines(screen())
    app.bot_data["state"] = {
        "prev": start, "anchor": settled(start)[-ANCHOR:],  # attach at the end: what is on screen now is not re-sent
        "queue": [], "last_change": now, "last_push": now, "idle_ticks": 0, "working": False,
    }
    app.bot_data["tail"] = {}
    app.bot_data["task"] = {}
    read_new(app.bot_data["tail"], session_info())  # attach at end of current transcript
    app.job_queue.run_repeating(tick, interval=1)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()

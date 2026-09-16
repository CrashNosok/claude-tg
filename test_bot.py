"""Run: uv run python test_bot.py"""
import json
from bot import TG_MAX, card, chunks, new_lines, task_log, user_prompt

assert card("<b>h</b>", "<x>") == "<b>h</b>\n<pre>&lt;x&gt;</pre>"
assert len(chunks("x" * (TG_MAX + 1))) == 2 and chunks("") == [""]

# streaming: locate the last sent lines (anchor) on the new capture; send only what follows and has settled
scr = ["❯ do x", "", "● line 1", "line 2 partial", "✽ Thinking…", "❯"]
assert new_lines(scr, scr, [], busy=True) == (["❯ do x", "", "● line 1"], ["❯ do x", "", "● line 1"])  # last text line held while busy
assert new_lines(scr, scr, ["● line 1"], busy=False) == (["line 2 partial"], ["● line 1", "line 2 partial"])  # idle: released
assert new_lines(scr, scr, ["● line 1", "line 2 partial"], busy=False) == ([], ["● line 1", "line 2 partial"])  # nothing new
grown = scr[:3] + ["line 2 partial words", "line 3", "✽ Kneading…", "❯"]
assert new_lines(scr, grown, ["● line 1"], busy=True) == ([], ["● line 1"])                       # seen once: not settled
assert new_lines(grown, grown, ["● line 1"], busy=True) == (["line 2 partial words"], ["● line 1", "line 2 partial words"])
scrolled = ["line 2 partial words", "line 3", "line 4", "✽ Kneading…", "❯"]                       # window moved: anchor still found
assert new_lines(scrolled, scrolled, ["● line 1", "line 2 partial words"], busy=True) == (["line 3"], ["● line 1", "line 2 partial words", "line 3"])
done = ["⏺ Bash(ls)", "  ⎿ a.txt", "❯"]                                                         # in-place rewrite: shorter anchor prefix
assert new_lines(done, done, ["⏺ Bash(ls)", "  ⎿ Running…"], busy=False) == (["  ⎿ a.txt"], ["⏺ Bash(ls)", "  ⎿ Running…", "  ⎿ a.txt"])
assert new_lines(["new"], ["new"], ["gone", "away"], busy=False) == (["new"], ["gone", "away", "new"])  # anchor gone: resend screen
assert new_lines([], ["x"], [], busy=False) == ([], [])                                         # not in previous capture yet

assert user_prompt({"type": "user", "message": {"content": "do x"}}) == "do x"
assert user_prompt({"type": "user", "message": {"content": [{"type": "tool_result", "content": "out"}]}}) is None
assert user_prompt({"type": "assistant", "message": {"content": "x"}}) is None
md = task_log({"prompt": "do x", "started": "now", "entries": [
    {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]}},
    {"type": "user", "message": {"content": [{"type": "tool_result", "content": "a.txt"}]}},
    {"type": "assistant", "message": {"content": [{"type": "text", "text": "done"}]}},
]})
assert "## Prompt" in md and "🔧 **Bash**" in md and "a.txt" in md and "**Claude:**\n\ndone" in md
print("ok")

# tmux targeting: exact session match + pane-level commands (needs tmux)
import os, subprocess, time
os.environ.update(TMUX_SESSION="cttest", TMUX_CMD="cat", TMUX_DIR="/tmp")
import importlib, bot; importlib.reload(bot)
subprocess.run(["tmux", "kill-session", "-t", "=cttest"], capture_output=True)
subprocess.run(["tmux", "new-session", "-d", "-s", "cttest-decoy", "cat"], check=True)   # prefix-collides with "cttest"
try:
    bot.ensure_session()
    assert subprocess.run(["tmux", "has-session", "-t", "=cttest"]).returncode == 0, "real session not created"
    bot.send("hello", literal=True); time.sleep(0.3)
    assert "hello" in bot.screen(), bot.screen()
finally:
    for s in ("cttest", "cttest-decoy"):
        subprocess.run(["tmux", "kill-session", "-t", "=" + s], capture_output=True)
print("ok tmux")

from bot import clean
raw = """ ▐▛███▛█   Claude Code v2.1.269
▝▜██████▀  Opus 5 (1M context) · Claude Max
  ▝▝ ▝▝    ~/projects/x · /rc


● hooks note

❯ Напиши текст   



✽ Kneading…

────────────────────────────────────────
❯ 
────────────────────────────────────────
  ⏵⏵ auto mode on (shift+tab to cycle) · esc to interrupt · ← for agents"""
c = clean(raw)
assert c == "● hooks note\n\n❯ Напиши текст\n\n✽ Kneading…\n\n❯", repr(c)
print("ok clean")

# anchor holds a line that vanished (typed prompt with nbsp, later re-rendered): newest surviving piece wins
old = ["a", "b", "gone", "❯ p", "", "● ans", "❯"]
assert new_lines(old, old, ["a", "b", "gone", "❯ p", ""], busy=False) == (["● ans"], ["a", "b", "gone", "❯ p", "", "● ans"])
from bot import clean_lines
assert clean_lines("❯\xa0hi  \n\n\n\nx") == ["❯ hi", "", "x"]
print("ok anchor")

from bot import drop_echo
assert drop_echo(["❯ /clear", "", "● ok", "❯\xa0/clear".replace("\xa0", " ")], ["/clear"]) == ["", "● ok"]
assert drop_echo(["❯ do x", "line"], ["do x\nsecond line"]) == ["line"]
print("ok echo")

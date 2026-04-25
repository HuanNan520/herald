# herald

A persona-driven Telegram bridge for [Claude Code](https://claude.com/claude-code) sessions.
Turns multiple Claude Code windows on a workstation into a single Telegram chat thread,
with an LLM middleware that speaks in a voice you define, batches noisy events,
and can even dispatch follow-up tasks back into those sessions.

> Built for a workflow where one person runs many concurrent Claude Code projects
> and doesn't want to tab-switch to see what they're all doing.

---

## What it does

```
┌──────────────────┐  Stop/Notification hook     ┌──────────────┐
│  Claude Code #1  │ ─────────────────────────▶  │   inbox/     │
│  Claude Code #2  │      (JSON events)          │  (JSON drop) │
│  Claude Code #N  │                             └──────┬───────┘
└──────────────────┘                                    │
                                                        ▼
┌──────────────────┐   sendMessage  ┌─────────────────────────────┐
│   Telegram bot   │ ◀───────────── │           daemon            │
│  (your private   │                │  • persona-voiced summaries │
│    chat w/ bot)  │ ─────────────▶ │  • adaptive debounce        │
└──────────────────┘    getUpdates  │  • slash commands           │
                                    │  • natural-language intent  │
                                    │  • @project dispatch        │
                                    │  • [[RUN:proj|cmd]] tool-   │
                                    │    call marker              │
                                    └─────────────┬───────────────┘
                                                  │
                                                  ▼  claude -p (subprocess)
                                    ┌─────────────────────────────┐
                                    │  Claude Code in any project │
                                    │     (any cwd you allow)     │
                                    └─────────────────────────────┘
```

**Key behaviours**

- **Event bus via filesystem** — hooks drop JSON into `inbox/`, daemon pulls. Hooks never
  block Claude Code; if the daemon is down, events pile up and flush on restart.
- **Adaptive debounce** — batches noisy `Stop`/`SubagentStop` events. Waits longer
  if you're away, shorter if you're actively chatting. Hard caps so nothing is ever
  lost forever.
- **Night-quiet mode** — `Notification` events (Claude waiting for input) are urgent
  by default, but get buffered when you haven't touched Telegram in a while, so your
  phone doesn't buzz at 4 a.m.
- **Slash commands** — `/status`, `/flush`, `/silence 30m`, `/off` (screen off),
  `/lock` (lock workstation). Natural language aliases too: "help me turn off the
  screen" → `/off`.
- **`@project: task` routing** — `@myblog: run lint and tell me what fails` spawns
  a `claude -p` inside that project and returns a persona-voiced summary.
- **Tool-call marker** — persona can emit `[[RUN:proj|cmd]]` inside a reply to
  delegate a sub-investigation. Marker is stripped before sending to Telegram;
  the real command runs in a background thread.
- **Supervisor + self-check** — `start_forever.sh` restarts the daemon on crash.
  `selfcheck.sh` is idempotent and safe to cron, with auto-repair paths for common
  issues (tmux missing, log bloat, processed backlog).

## Where this fits in 2026

Built before [Claude Code Channels](https://code.claude.com/docs/en/channels)
shipped (March 20, 2026). Different shape of problem.

|                                   | Channels | claude-code-telegram | Claude Squad | **herald** |
|-----------------------------------|:--------:|:--------------------:|:------------:|:----------:|
| Telegram ↔ Claude                 |    ✓    |          ✓          |       —      |     ✓     |
| Multi-session aggregation         |   1:1   |         1:1          |       —      |   **N:1**  |
| Persona-voiced summary            |    —    |          —           |       —      |   **✓**   |
| Persona dispatches sub-tasks      |    —    |          —           |       —      |   **✓**   |
| Adaptive debounce / quiet hours   |    —    |          —           |       —      |   **✓**   |
| Multi-agent orchestration         |    —    |          —           |      ✓      |      —     |
| Official Anthropic support        |    ✓    |          —           |       —      |      —     |

**If you have one Claude Code session** and want a phone-side terminal,
use Channels. That's the official path now and it's well supported.

**If you have many concurrent Claude Code windows** running across different
projects and want them aggregated through a voice you own — with the AI
deciding when to surface and when to stay quiet — that's what this does.

The design predates Channels and is built on Claude Code's `Stop` / `SubagentStop` /
`Notification` hooks plus `claude -p` subprocesses, not MCP. Tradeoffs noted under
[Status & caveats](#status--caveats).

## Requirements

- [Claude Code CLI](https://claude.com/claude-code) installed and logged in
  (any plan that gives you `claude -p` access works)
- Python 3.9+ (stdlib only, no deps)
- `tmux` (for the supervisor)
- A Telegram bot token and your numeric chat ID
- Works best on Linux / WSL. The `/off` and `/lock` slash commands call
  `powershell.exe`, so they only do something useful on WSL.

## Quick start

```bash
git clone https://github.com/HuanNan520/herald.git
cd herald

# 1. configure
cp .env.example .env
$EDITOR .env                    # fill in TG_BOT_TOKEN, TG_CHAT_ID, USER_NAME, BOT_NAME

cp persona.example.txt persona.txt
$EDITOR persona.txt             # write your bot's voice

cp projects.example.json projects.json   # optional: set search paths + aliases

# 2. wire Claude Code hooks
# Merge settings.example.json's "hooks" block into ~/.claude/settings.json
# (see that file for the minimum you need)

# 3. run
tmux new -d -s herald "bash scripts/start_forever.sh"

# 4. self-check (ok to cron this every 5 minutes)
bash scripts/selfcheck.sh
```

Once started, the bot should send a greeting into your Telegram chat. Reply with
`/help` to see all commands.

## Configuration

All config is via env vars (`.env` file is sourced by `scripts/start.sh`):

| Var | Default | Notes |
|---|---|---|
| `TG_BOT_TOKEN` | *(required)* | From [@BotFather](https://t.me/BotFather) |
| `TG_CHAT_ID` | *(required)* | Your numeric chat ID. Talk to [@userinfobot](https://t.me/userinfobot) |
| `HERALD_HOME` | `$HOME/.herald` | Where inbox/pending/processed/state live |
| `PERSONA_FILE` | `$HERALD_HOME/persona.txt` | Your persona definition |
| `PROJECTS_FILE` | `$HERALD_HOME/projects.json` | Optional: search paths + aliases |
| `USER_NAME` | `user` | How the bot refers to you in prompts |
| `BOT_NAME` | `assistant` | The bot's name (for the persona's self-reference) |
| `CLAUDE_MODEL` | `claude-opus-4-7` | Model for persona generation |
| `DEBOUNCE_MIN_SECS` | `300` | Minimum batch wait (5 min) |
| `DEBOUNCE_MAX_SECS` | `3600` | Maximum batch wait when you're silent |

## Slash commands

| Command | What it does |
|---|---|
| `/status` | Show pending buffer, debounce level, user-active flag, silence timer |
| `/flush` | Summarize pending events right now (bypass debounce) |
| `/wake` | Reset debounce to baseline, clear silence |
| `/silence 30m` | Mute for 30m (also `1h`, `120s`) |
| `/unmute` | End silence early |
| `/off` | Blank the monitor (Windows only, via WSL→powershell.exe) |
| `/lock` | Lock workstation (Windows only) |
| `/help` | Print command list |
| `@project: task` | Dispatch `claude -p` into the named project |

Natural-language detection is registered for the common ones, e.g. "blank my
screen" / "关掉屏幕" both map to `/off`.

## Persona customisation

The persona is a plain text file. The daemon prepends it to every generation.
See `persona.example.txt` for a starting point. Key tips:

- **Don't hardcode a greeting.** Let the model improvise per invocation.
- **Define *tone*, not *template sentences*.** Models paraphrase templates badly;
  they nail tone.
- **Tell it about the tool-call marker.** Give concrete examples.
  Otherwise it will only "agree" to do things, never actually dispatch.
- **Tell it how to handle completion events.** If you don't, it defaults to
  "task done", which is useless. Prompt it to extract what Claude *actually did*
  from the transcript excerpt.

## How the persona calls tools

On each chat reply, the model's output is scanned for the literal regex
`[[RUN:<project>|<command>]]`. Each match is:

1. stripped from the visible message;
2. dispatched as `claude -p <command>` inside the resolved project dir
   (fuzzy-matched: exact → alias → substring);
3. the output gets run back through the persona for a friendly summary and
   sent as a separate Telegram message.

This lets the persona say "I'll go check" and actually go check.

## Architecture notes

- **Recursion guard.** The daemon sets `HERALD_SILENCE=1` in the subprocess
  environment when it spawns `claude -p`. The hook checks that variable and
  exits early. Without this, a sub-`claude` call triggers a Stop hook which
  writes to inbox which the daemon reads which triggers *another* `claude -p` — a
  fork bomb built out of LLM calls. Don't ask how we know.
- **PID file, not `pgrep`.** The daemon writes `daemon.pid` and the supervisor
  checks with `kill -0`. `pgrep -f "daemon.py"` misses because the cmdline is
  just `python3 daemon.py` after a `cd`.
- **State persistence.** `state.json` holds Telegram update offset, debounce
  level, last-batch timestamp, last-user-msg timestamp, silence-until. Daemon
  restarts without losing these. `history.json` holds the last ~20 turns (1h
  window) so the persona can reference prior context.
- **Supervisor log hygiene.** `start_forever.sh` redirects daemon stderr to a
  separate `daemon.err` rather than appending to `daemon.log`. Otherwise
  every line in `daemon.log` would show up twice (daemon writes it once, then
  stderr catches it again via the supervisor's `2>&1`).

## Status & caveats

This is a personal tool open-sourced so others can adapt it. It is:

- **Single-user by design.** The daemon whitelists one `TG_CHAT_ID`. It is not
  a multi-tenant bot.
- **Written against Claude Code CLI, not the Anthropic API.** If you prefer
  direct API, `call_claude()` is ~20 lines; swap in an `anthropic.Anthropic`
  client. Keep in mind any plan-level TOS for headless/daemonised CLI use.
- **Designed for WSL + Windows.** Nothing Linux-hostile, but `/off` and `/lock`
  only do work via `powershell.exe`.
- **Pre-1.0.** No tests yet. I use it daily; you should read the code before
  trusting it with cross-project dispatch.

## License

MIT. See `LICENSE`.

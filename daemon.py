#!/usr/bin/env python3
"""herald daemon.

Reads JSON events from `inbox/` (dropped by the Claude Code hook), renders them
through an LLM persona, and delivers them to a single Telegram chat. Listens
for incoming Telegram messages and can dispatch `claude -p` sub-tasks into
any project on the machine.

See README.md for architecture details.
"""

import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path


# ─── Config from env ────────────────────────────────────────────────────────

HERALD_HOME = Path(
    os.environ.get("HERALD_HOME", str(Path.home() / ".herald"))
).expanduser()
PERSONA_FILE = Path(
    os.environ.get("PERSONA_FILE", str(HERALD_HOME / "persona.txt"))
).expanduser()
PROJECTS_FILE = Path(
    os.environ.get("PROJECTS_FILE", str(HERALD_HOME / "projects.json"))
).expanduser()

USER_NAME = os.environ.get("USER_NAME", "user")
BOT_NAME = os.environ.get("BOT_NAME", "assistant")
MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-4-7")

TOKEN = os.environ.get("TG_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TG_CHAT_ID", "")
if not TOKEN or not CHAT_ID:
    print("TG_BOT_TOKEN / TG_CHAT_ID not set", file=sys.stderr)
    sys.exit(1)

# Adaptive debounce: short → medium → long.
# After each batch send, if the user hasn't replied, the next debounce steps up.
DEBOUNCE_MIN = int(os.environ.get("DEBOUNCE_MIN_SECS", "300"))     # 5 min
DEBOUNCE_MID = int(os.environ.get("DEBOUNCE_MID_SECS", "1200"))    # 20 min
DEBOUNCE_MAX = int(os.environ.get("DEBOUNCE_MAX_SECS", "3600"))    # 1 h
DEBOUNCE_LEVELS = [DEBOUNCE_MIN, DEBOUNCE_MID, DEBOUNCE_MAX]

# Hard caps in case the event stream never goes quiet.
MAX_BATCH_WAIT_ACTIVE = int(os.environ.get("MAX_BATCH_WAIT_ACTIVE", "1800"))    # 30 min
MAX_BATCH_WAIT_INACTIVE = int(os.environ.get("MAX_BATCH_WAIT_INACTIVE", "7200"))  # 2 h
MAX_PENDING_COUNT = int(os.environ.get("MAX_PENDING_COUNT", "12"))

USER_ACTIVE_WINDOW = int(os.environ.get("USER_ACTIVE_WINDOW", "1800"))

URGENT_EVENTS = {"Notification"}
BUFFERED_EVENTS = {"Stop", "SubagentStop"}

HISTORY_MAX = 20
HISTORY_WINDOW_SECS = 3600
HISTORY_RENDER_N = 10

# Tool-call marker: the persona writes [[RUN:project|command]] inside replies,
# we strip it from the visible text and actually dispatch `claude -p` for it.
TOOL_CALL_RE = re.compile(r"\[\[RUN:([^|\]]+)\|([^\]]+)\]\]")


# ─── Runtime paths ──────────────────────────────────────────────────────────

INBOX = HERALD_HOME / "inbox"
PENDING = HERALD_HOME / "pending"
PROCESSED = HERALD_HOME / "processed"
STATE = HERALD_HOME / "state.json"
LOG = HERALD_HOME / "daemon.log"
PIDFILE = HERALD_HOME / "daemon.pid"
HISTORY_FILE = HERALD_HOME / "history.json"

TG_API = f"https://api.telegram.org/bot{TOKEN}"


# ─── Persona + projects ─────────────────────────────────────────────────────

if not PERSONA_FILE.exists():
    print(f"ERROR: persona file not found at {PERSONA_FILE}", file=sys.stderr)
    print("Copy persona.example.txt there and customize it.", file=sys.stderr)
    sys.exit(1)

PERSONA = (
    PERSONA_FILE.read_text(encoding="utf-8")
    .replace("{{BOT_NAME}}", BOT_NAME)
    .replace("{{USER_NAME}}", USER_NAME)
)

PROJECT_SEARCH_PATHS = []
PROJECT_ALIASES = {}

if PROJECTS_FILE.exists():
    try:
        pdata = json.loads(PROJECTS_FILE.read_text(encoding="utf-8"))
        for p in pdata.get("search_paths", []):
            PROJECT_SEARCH_PATHS.append(Path(p).expanduser())
        PROJECT_ALIASES = {
            k.lower(): v
            for k, v in pdata.get("aliases", {}).items()
            if not k.startswith("_")
        }
    except Exception as e:
        print(f"WARN: failed to parse {PROJECTS_FILE}: {e}", file=sys.stderr)

if not PROJECT_SEARCH_PATHS:
    PROJECT_SEARCH_PATHS = [Path.home(), Path.home() / "projects"]


# ─── Mutable state ──────────────────────────────────────────────────────────

pending_events = []
last_pending_ts = 0.0
first_pending_ts = 0.0        # for the MAX_BATCH_WAIT hard cap
last_batch_sent_ts = 0.0      # >0 means last batch sent and user hasn't replied yet
last_user_msg_ts = 0.0        # last incoming TG message time (user-active heuristic)
silence_until = 0.0           # manual mute cutoff (/silence)
debounce_level = 0
history = []                  # [{role: user|assistant|event|batch, content, ts}]


def log(msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n"
    LOG.open("a", encoding="utf-8").write(line)
    # Don't print to stderr — supervisor captures stderr to daemon.err separately,
    # otherwise every log line ends up duplicated.


# ─── Activity / silence helpers ─────────────────────────────────────────────

def is_silenced():
    return silence_until > 0 and time.time() < silence_until


def is_user_active():
    """Did the user send a TG message in the last USER_ACTIVE_WINDOW seconds?
    Forcibly false during manual silence."""
    if is_silenced():
        return False
    if last_user_msg_ts == 0:
        return False
    return (time.time() - last_user_msg_ts) < USER_ACTIVE_WINDOW


def current_debounce():
    base = DEBOUNCE_LEVELS[min(debounce_level, len(DEBOUNCE_LEVELS) - 1)]
    if is_silenced():
        # During silence, only the MAX_BATCH_WAIT cap will trigger a flush.
        return max(base, DEBOUNCE_MAX)
    if not is_user_active():
        base = max(base, DEBOUNCE_MID)
    return base


# ─── State persistence ─────────────────────────────────────────────────────

def load_state():
    if STATE.exists():
        try:
            return json.loads(STATE.read_text())
        except json.JSONDecodeError:
            pass
    return {
        "tg_update_offset": 0,
        "debounce_level": 0,
        "last_batch_sent_ts": 0.0,
    }


def save_state(s):
    s["debounce_level"] = debounce_level
    s["last_batch_sent_ts"] = last_batch_sent_ts
    s["last_user_msg_ts"] = last_user_msg_ts
    s["silence_until"] = silence_until
    STATE.write_text(json.dumps(s))


# ─── Telegram HTTP ─────────────────────────────────────────────────────────

def tg_request(method, params=None, post=False, timeout=35):
    url = f"{TG_API}/{method}"
    try:
        if post:
            data = urllib.parse.urlencode(params or {}).encode()
            req = urllib.request.Request(url, data=data)
        else:
            if params:
                url += "?" + urllib.parse.urlencode(params)
            req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception as e:
        log(f"tg {method} failed: {e}")
        return None


def send_tg(text):
    if not text:
        return
    if len(text) > 4000:
        text = text[:3990] + "…(trunc)"
    tg_request("sendMessage", {"chat_id": CHAT_ID, "text": text}, post=True)


# ─── History ───────────────────────────────────────────────────────────────

def save_history():
    try:
        HISTORY_FILE.write_text(
            json.dumps(history, ensure_ascii=False), encoding="utf-8"
        )
    except Exception as e:
        log(f"save_history failed: {e}")


def load_history():
    if not HISTORY_FILE.exists():
        return
    try:
        data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            return
        cutoff = time.time() - HISTORY_WINDOW_SECS
        kept = [h for h in data if isinstance(h, dict) and h.get("ts", 0) >= cutoff]
        if len(kept) > HISTORY_MAX:
            kept = kept[-HISTORY_MAX:]
        history.clear()
        history.extend(kept)
        if history:
            log(f"history restored: {len(history)} entries")
    except Exception as e:
        log(f"load_history failed: {e}")


def record_history(role, content):
    history.append({"role": role, "content": content, "ts": time.time()})
    cutoff = time.time() - HISTORY_WINDOW_SECS
    while history and (history[0]["ts"] < cutoff or len(history) > HISTORY_MAX):
        history.pop(0)
    save_history()


def render_context():
    if not history:
        return "(no prior messages)"
    lines = []
    for h in history[-HISTORY_RENDER_N:]:
        c = h["content"][:300]
        role = h["role"]
        if role == "user":
            lines.append(f"{USER_NAME}: {c}")
        elif role == "assistant":
            lines.append(f"you ({BOT_NAME}): {c}")
        elif role == "event":
            lines.append(f"[event] {c}")
        elif role == "batch":
            lines.append(f"[batch summary] {c}")
    return "\n".join(lines)


# ─── Telegram message → text ───────────────────────────────────────────────

def extract_message_text(msg):
    """Fold any Telegram message type into a single text line the persona can read."""
    text = (msg.get("text") or "").strip()
    if text:
        return text
    if "sticker" in msg:
        emoji = msg["sticker"].get("emoji", "")
        set_name = msg["sticker"].get("set_name", "")
        return f"[sent a sticker: {emoji} ({set_name})]" if emoji else "[sent a sticker]"
    if "animation" in msg:
        return "[sent an animation/GIF]"
    if "photo" in msg:
        cap = msg.get("caption", "").strip()
        return f"[sent a photo: {cap}]" if cap else "[sent a photo]"
    if "voice" in msg:
        return "[sent a voice message]"
    if "video" in msg:
        return "[sent a video]"
    if "document" in msg:
        return "[sent a file]"
    return ""


# ─── Transcript parsing ────────────────────────────────────────────────────

def extract_last_claude_output(transcript_path, max_chars=800):
    """Pull the last assistant text block from a Claude Code session transcript."""
    if not transcript_path:
        return ""
    p = Path(transcript_path)
    if not p.exists():
        return ""
    try:
        lines = p.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return ""
    for line in reversed(lines):
        try:
            obj = json.loads(line)
        except Exception:
            continue
        # Compatible with a few transcript schemas
        role = (
            obj.get("role")
            or obj.get("type")
            or (obj.get("message") or {}).get("role")
        )
        if role not in ("assistant",):
            continue
        body = obj.get("message") or obj
        content = body.get("content") if isinstance(body, dict) else None
        text = ""
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        parts.append(block.get("text", ""))
                    elif block.get("type") == "thinking":
                        continue
                elif isinstance(block, str):
                    parts.append(block)
            text = "\n".join(p for p in parts if p)
        text = text.strip()
        if text:
            return text[:max_chars]
    return ""


# ─── Claude CLI wrapper ────────────────────────────────────────────────────

def call_claude(prompt, cwd=None, timeout=600):
    env = os.environ.copy()
    # Prevents the sub-`claude` from triggering our own hook,
    # which would cause infinite LLM-call recursion.
    env["HERALD_SILENCE"] = "1"
    try:
        r = subprocess.run(
            ["claude", "-p", "--model", MODEL, prompt],
            cwd=cwd or str(Path.home()),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        if r.returncode != 0:
            log(f"claude returncode={r.returncode} stderr={r.stderr[:300]}")
            return f"({BOT_NAME} errored: {r.stderr[:200].strip()})"
        return r.stdout.strip()
    except subprocess.TimeoutExpired:
        return f"({BOT_NAME} timed out)"
    except Exception as e:
        log(f"claude exception: {e}")
        return f"({BOT_NAME} crashed: {e})"


def persona_say(brief, extra="", long=False):
    constraint = (
        "You may go longer (under ~300 words), report project-by-project in your voice. "
        "Still one message only — do not split into multiple replies."
        if long
        else "Keep it under 3 sentences. One message only."
    )
    ctx = render_context()
    prompt = (
        f"{PERSONA}\n\n"
        "---\n\n"
        f"Recent conversation (for tone/context, don't parrot):\n{ctx}\n\n"
        "---\n\n"
        f"Below is a Claude Code event. In your voice as {BOT_NAME}, tell {USER_NAME} "
        f"what happened. {constraint}\n\n"
        f"Event:\n{brief}"
    )
    if extra:
        prompt += f"\n\nAdditional context:\n{extra[:3500]}"
    return call_claude(prompt)


def chat_reply(user_text):
    ctx = render_context()
    prompt = (
        f"{PERSONA}\n\n"
        "---\n\n"
        f"Recent conversation (for tone/context, don't parrot):\n{ctx}\n\n"
        "---\n\n"
        f"{USER_NAME} just said: \"{user_text}\"\n\n"
        "Reply in your voice. Under 3 sentences, one message only.\n"
        "\n"
        "# Dispatching real work\n"
        f"If {USER_NAME} asks you to actually DO something in a project — query it, "
        "run a command there, etc. — you MUST include a tool-call marker in your reply:\n"
        "  [[RUN:<project>|<what you want Claude to do>]]\n"
        "Examples:\n"
        f"- {USER_NAME}: \"ask blog what it committed today\"\n"
        "  you: \"fine. [[RUN:blog|List today's commits with one-line summaries.]]\"\n"
        f"- {USER_NAME}: \"check whether game's tests pass\"\n"
        "  you: \"on it. [[RUN:game|Run the test suite and report pass/fail counts.]]\"\n"
        "\n"
        f"The marker is stripped before {USER_NAME} sees the reply; the command runs in "
        "the resolved project's cwd via `claude -p` in a background thread, and its "
        "result comes back as a separate Telegram message.\n"
        "Do NOT emit a marker for chit-chat, opinions, or support — only when the user "
        "wants a thing done.\n"
        "Project name can be a literal dir name, an alias, or a fuzzy substring.\n"
    )
    return call_claude(prompt)


def extract_tool_calls(text):
    """Return (visible_text_with_markers_stripped, [(project, command), ...])"""
    calls = []

    def _replace(m):
        project = m.group(1).strip().lstrip("@")
        command = m.group(2).strip()
        if project and command:
            calls.append((project, command))
        return ""

    clean = TOOL_CALL_RE.sub(_replace, text).strip()
    return clean, calls


# ─── Event routing ─────────────────────────────────────────────────────────

def transform_notification(event):
    ev = event.get("hook_event_name", "?")
    project = event.get("project", "?")
    session = event.get("session", "")
    raw_msg = event.get("message") or ""
    last_said = extract_last_claude_output(event.get("transcript_path", ""), 500)

    kind_map = {
        "Stop": "The Claude in this project finished a turn (task round complete)",
        "Notification": "The Claude in this project is waiting on user input "
                        "(permission prompt or idle)",
        "SubagentStop": "A sub-agent in this project completed",
    }
    desc = kind_map.get(ev, f"event: {ev}")
    brief = f"{desc}\nproject: {project}\nsession: {session}"
    if raw_msg:
        brief += f"\nraw system message: {raw_msg}"
    if last_said:
        brief += f"\nClaude's last words (extract, don't copy verbatim):\n  \"{last_said}\""
    return persona_say(brief)


def summarize_and_send():
    global last_batch_sent_ts, debounce_level, pending_events, first_pending_ts
    if not pending_events:
        return

    # Step up debounce if the last batch wasn't answered.
    if last_batch_sent_ts > 0:
        new_level = min(debounce_level + 1, len(DEBOUNCE_LEVELS) - 1)
        if new_level != debounce_level:
            log(
                f"no reply to last batch → debounce {debounce_level} → {new_level} "
                f"({DEBOUNCE_LEVELS[new_level] // 60}min)"
            )
        debounce_level = new_level

    events = list(pending_events)

    # Group events by project for the prompt.
    by_project = {}
    for e in events:
        p = e.get("project", "?")
        by_project.setdefault(p, []).append(e)

    n_stop = sum(1 for e in events if e.get("hook_event_name") == "Stop")
    n_notif = sum(1 for e in events if e.get("hook_event_name") == "Notification")
    n_sub = sum(1 for e in events if e.get("hook_event_name") == "SubagentStop")

    group_lines = []
    for project, evs in by_project.items():
        evs_sorted = sorted(evs, key=lambda x: x.get("timestamp", 0))
        group_lines.append(f"## [{project}] ({len(evs)} event(s))")
        for e in evs_sorted:
            ts = e.get("timestamp", 0)
            t = time.strftime("%H:%M", time.localtime(ts)) if ts else "??:??"
            ev = e.get("hook_event_name", "?")
            raw_msg = e.get("message") or ""
            last_said = extract_last_claude_output(e.get("transcript_path", ""), 400)
            if ev == "Notification":
                entry = f"- {t} (⏸ waiting-for-input)"
                if raw_msg:
                    entry += f" — {raw_msg[:80]}"
                if last_said:
                    entry += f"\n  last said: \"{last_said}\""
            elif ev == "SubagentStop":
                entry = f"- {t} (🤖 sub-agent done)"
                if last_said:
                    entry += f" — last said: \"{last_said}\""
            else:  # Stop
                entry = f"- {t} (turn complete)"
                if last_said:
                    entry += f" — last said: \"{last_said}\""
                else:
                    entry += " — (no transcript excerpt)"
            group_lines.append(entry)

    type_summary_parts = []
    if n_stop:
        type_summary_parts.append(f"{n_stop} turn-complete")
    if n_notif:
        type_summary_parts.append(f"{n_notif} waiting-for-input")
    if n_sub:
        type_summary_parts.append(f"{n_sub} sub-agent done")
    type_summary = ", ".join(type_summary_parts)

    brief = (
        f"No new events for the last {current_debounce() // 60} minutes. "
        f"Window total: {len(events)} event(s) ({type_summary}) across "
        f"{len(by_project)} project(s):\n\n"
        + "\n\n".join(group_lines)
        + "\n\nRequirements:\n"
        "1. Report **project by project**, blank lines between projects.\n"
        "2. For each project:\n"
        "   - Turn-complete: say what the Claude there ACTUALLY did "
        "(extract from the excerpt, don't quote verbatim). If it hinted at "
        "next steps, include that.\n"
        "   - **⏸ waiting-for-input** events: CALL THEM OUT explicitly — "
        f"'this one is blocked on {USER_NAME}'. These won't progress until "
        "the user answers directly.\n"
        "   - Sub-agent done: judge importance and detail accordingly.\n"
        "3. Do NOT say 'done' / 'completed' without specifics.\n"
        "4. Stay in your persona voice — not formal report mode.\n"
        "5. One message only, no splits.\n"
        "6. If you want to dig into a project further, you MAY emit "
        "[[RUN:<project>|<question>]] to dispatch your own follow-up "
        "(git log, status check, etc.).\n"
        "   IMPORTANT: only for YOUR OWN investigation. Do NOT dispatch "
        "anything to the ⏸ waiting-for-input windows — those need the user's "
        "direct attention, not a background subprocess."
    )

    summary = persona_say(brief, long=True)
    clean_summary, tool_calls = extract_tool_calls(summary)
    if clean_summary:
        send_tg(clean_summary)
        record_history("batch", f"({len(events)} events) {clean_summary[:150]}")
    for project, command in tool_calls:
        log(f"batch summary tool-call → @{project}: {command[:80]}")
        handle_command(project, command)
    log(
        f"batch sent: {len(events)} events → 1 summary "
        f"(level={debounce_level}, next_debounce={current_debounce()//60}min)"
    )

    for e in events:
        src = e.get("_src")
        if src:
            try:
                Path(src).rename(PROCESSED / Path(src).name)
            except Exception:
                pass

    pending_events = []
    last_batch_sent_ts = time.time()
    first_pending_ts = 0.0


# ─── Project dispatch ──────────────────────────────────────────────────────

def find_project_dir(name):
    """Resolve project name → absolute dir. Order: exact, alias, substring."""
    name = name.strip().lstrip("@")
    if not name:
        return None

    # 1. exact match under any search path
    for base in PROJECT_SEARCH_PATHS:
        if not base.exists():
            continue
        p = base / name
        if p.is_dir():
            return str(p)

    # 2. alias
    if name.lower() in PROJECT_ALIASES:
        return find_project_dir(PROJECT_ALIASES[name.lower()])

    # 3. substring
    name_lower = name.lower()
    candidates = []
    for base in PROJECT_SEARCH_PATHS:
        if not base.exists():
            continue
        try:
            for p in base.iterdir():
                if not p.is_dir():
                    continue
                if name_lower in p.name.lower():
                    candidates.append(p)
        except Exception:
            continue

    if candidates:
        # Prefer the shortest match — less likely to be a nested dir.
        best = min(candidates, key=lambda x: len(x.name))
        return str(best)

    return None


def _handle_command_worker(project, command):
    """Runs in a background thread so the TG poll loop stays responsive."""
    cwd = find_project_dir(project)
    if not cwd:
        # Gather suggestions from nearby dirs so the persona can offer alternatives.
        all_names = set()
        for base in PROJECT_SEARCH_PATHS:
            if not base.exists():
                continue
            try:
                for p in base.iterdir():
                    if p.is_dir() and not p.name.startswith("."):
                        all_names.add(p.name)
            except Exception:
                continue
        import difflib
        suggestions = difflib.get_close_matches(
            project, list(all_names), n=3, cutoff=0.4
        )
        suggestions_str = ", ".join(suggestions) if suggestions else "(none close)"

        ctx = render_context()
        reply = call_claude(
            f"{PERSONA}\n\n---\n\n"
            f"Recent context:\n{ctx}\n\n---\n\n"
            f"{USER_NAME} asked you to run in project '{project}': \"{command}\". "
            f"No match — not exact, not alias, not substring. "
            f"Closest guesses: {suggestions_str}.\n"
            f"In your voice, tell {USER_NAME} you couldn't find it, and ask whether "
            "they meant one of the suggestions (if any) or want to give a full path. "
            "One sentence."
        )
        if not reply or reply.startswith("("):
            reply = f"Couldn't find '{project}'. Did you mean: {suggestions_str}?"
        send_tg(reply)
        record_history("assistant", reply)
        return

    raw_output = call_claude(command, cwd=cwd, timeout=900)
    summary = persona_say(
        f"{USER_NAME} asked you to run '{command}' in project '{project}'. "
        "Below is Claude's raw output. In your voice, tell them what Claude "
        "actually did; if Claude hinted at a next step, include that. "
        "Don't say 'done' without specifics. One message only.",
        extra=raw_output,
        long=True,
    )
    send_tg(summary)
    record_history("assistant", summary[:200])


def handle_command(project, command):
    """Fire and forget — TG loop keeps polling while claude -p works."""
    t = threading.Thread(
        target=_handle_command_worker,
        args=(project, command),
        daemon=True,
        name=f"cmd-{project}",
    )
    t.start()
    log(f"dispatched @{project}: {command[:60]}")


# ─── Windows integration (WSL only) ────────────────────────────────────────

def run_windows_cmd(ps_cmd, timeout=10):
    """Run a powershell.exe command from WSL. Returns (ok, trimmed_output)."""
    try:
        r = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
            capture_output=True, text=True, timeout=timeout,
        )
        return (r.returncode == 0, (r.stdout + r.stderr).strip()[:400])
    except Exception as e:
        return (False, f"exc: {e}")


# ─── Slash commands ────────────────────────────────────────────────────────

def handle_slash_command(text):
    """Returns True if the text was consumed as a slash command."""
    global debounce_level, last_batch_sent_ts, silence_until
    parts = text.strip().split(maxsplit=1)
    cmd = parts[0].lower()
    arg = parts[1] if len(parts) > 1 else ""

    if cmd == "/wake":
        debounce_level = 0
        last_batch_sent_ts = 0.0
        silence_until = 0.0
        send_tg("Debounce reset, silence cleared.")
        log("/wake: debounce reset")
        return True

    if cmd in ("/silence", "/mute", "/quiet"):
        m = re.match(r"^(\d+)\s*([smh]?)\s*$", arg.strip())
        if not m:
            send_tg("Usage: /silence 30m  |  /silence 2h  |  /silence 1800  (s/m/h)")
            return True
        n = int(m.group(1))
        unit = m.group(2) or "m"
        secs = {"s": n, "m": n * 60, "h": n * 3600}[unit]
        silence_until = time.time() + secs
        send_tg(f"Muted for {n}{unit}. /wake to lift early.")
        log(f"/silence {n}{unit}: silence_until=+{secs}s")
        return True

    if cmd in ("/unmute", "/loud"):
        silence_until = 0.0
        send_tg("Unmuted.")
        log("/unmute")
        return True

    if cmd in ("/off", "/monitoroff", "/screenoff"):
        ok, out = run_windows_cmd(
            "(Add-Type '[DllImport(\"user32.dll\")]public static extern int "
            "SendMessage(int a,int b,int c,int d);' -Name A -PassThru)"
            "::SendMessage(-1,0x0112,0xF170,2)"
        )
        if ok:
            send_tg("Screen off.")
            log("/off: screen blanked")
        else:
            send_tg(f"(couldn't blank screen: {out[:200]})")
            log(f"/off failed: {out[:200]}")
        return True

    if cmd == "/lock":
        ok, out = run_windows_cmd("rundll32.exe user32.dll,LockWorkStation")
        if ok:
            send_tg("Locked.")
            log("/lock")
        else:
            send_tg(f"(couldn't lock: {out[:200]})")
        return True

    if cmd in ("/status", "/stat"):
        nowt = time.time()
        lines = [
            f"= {BOT_NAME} status =",
            f"pending: {len(pending_events)}",
        ]
        if pending_events:
            if first_pending_ts > 0:
                lines.append(f"oldest event: {int(nowt - first_pending_ts)}s ago")
            if last_pending_ts > 0:
                lines.append(f"newest event: {int(nowt - last_pending_ts)}s ago")
            need = current_debounce() - (nowt - last_pending_ts)
            if need > 0:
                lines.append(
                    f"needs {int(need)}s more silence to batch "
                    f"(current debounce {current_debounce()//60}min)"
                )
            else:
                lines.append("should batch any moment now…")
        lines.append(f"debounce level: {debounce_level}")
        active = is_user_active()
        silenced = is_silenced()
        lines.append(f"user: {'active' if active else 'away'}"
                     f"{' (muted)' if silenced else ''}")
        if silenced:
            lines.append(f"  mute ends in: {int(silence_until - nowt)}s")
        if last_user_msg_ts > 0:
            lines.append(f"last TG msg: {int((nowt - last_user_msg_ts)/60)}min ago")
        if last_batch_sent_ts > 0:
            lines.append(f"last batch: {int((nowt - last_batch_sent_ts)/60)}min ago")
        inbox_n = len(list(INBOX.glob("*.json")))
        proc_n = len(list(PROCESSED.glob("*.json")))
        lines.append(f"inbox={inbox_n} processed={proc_n}")
        lines.append(f"history: {len(history)} entries")
        send_tg("\n".join(lines))
        return True

    if cmd in ("/flush", "/now"):
        if not pending_events:
            send_tg("(pending buffer empty, nothing to summarise)")
        else:
            n = len(pending_events)
            summarize_and_send()
            log(f"/flush: forced batch of {n}")
        return True

    if cmd == "/help":
        send_tg(
            "= commands =\n"
            "/status       - pending / debounce / silence state\n"
            "/flush        - summarise pending now\n"
            "/wake         - reset debounce + unmute\n"
            "/silence 30m  - mute for 30 min (s/m/h units)\n"
            "/unmute       - end silence early\n"
            "/off          - blank Windows monitor (WSL only)\n"
            "/lock         - lock Windows workstation (WSL only)\n"
            "/help         - this\n"
            "\n"
            "= natural language =\n"
            "'blank the screen', 'lock it', 'tell me now' all work\n"
            "\n"
            "= project dispatch =\n"
            "@project: task   - run claude -p inside that project\n"
            "plain text        - chat with the persona\n"
        )
        return True

    return False


def detect_intent(text):
    """Natural-language mapping to slash commands. Bilingual keywords
    (EN + zh-CN) on by default; extend as needed for your locale."""
    t = text.replace(" ", "").lower()

    for kw in (
        "blankthescreen", "screenoff", "turnoffthescreen", "monitoroff",
        "熄屏", "熄灭屏幕", "关屏", "关掉屏幕", "屏幕关", "关显示器",
        "黑屏", "灭屏",
    ):
        if kw in t:
            return "/off"

    for kw in ("lockscreen", "lockit", "lockworkstation",
               "锁屏", "锁机", "锁电脑", "锁上"):
        if kw in t:
            return "/lock"

    for kw in ("flushnow", "tellmenow", "rightnow", "forceflush",
               "马上汇报", "立刻汇报", "快告诉我", "flush", "强制汇报"):
        if kw in t:
            return "/flush"

    for kw in ("whatsthestatus", "currentstate", "howmanypending",
               "什么状态", "啥状态", "现在状况", "积了多少", "堆积了多少"):
        if kw in t:
            return "/status"

    return None


# ─── TG message handling ───────────────────────────────────────────────────

def handle_tg_message(msg):
    global last_batch_sent_ts, debounce_level, last_user_msg_ts

    text = extract_message_text(msg)
    if not text:
        return

    log(f"<- TG: {text[:100]}")
    record_history("user", text)
    last_user_msg_ts = time.time()

    # User activity resets debounce back to baseline.
    if last_batch_sent_ts > 0:
        if debounce_level > 0:
            log(f"user replied → debounce {debounce_level} → 0")
        debounce_level = 0
        last_batch_sent_ts = 0

    # Slash first (no LLM call).
    if text.startswith("/"):
        if handle_slash_command(text):
            return

    # Natural-language intent detection (so users don't have to remember slash).
    intent = detect_intent(text)
    if intent:
        log(f"intent detected: {text[:40]} → {intent}")
        handle_slash_command(intent)
        return

    # @project: command routing
    if text.startswith("@"):
        parts = text[1:].split(":", 1)
        if len(parts) == 2:
            project, command = parts[0].strip(), parts[1].strip()
            if project and command:
                handle_command(project, command)
                return

    # Otherwise chat with the persona.
    reply = chat_reply(text)
    clean_reply, tool_calls = extract_tool_calls(reply)
    if clean_reply:
        send_tg(clean_reply)
        record_history("assistant", clean_reply)
    for project, command in tool_calls:
        log(f"persona tool-call → @{project}: {command[:80]}")
        handle_command(project, command)


# ─── Main loops ────────────────────────────────────────────────────────────

def poll_inbox():
    global last_pending_ts, first_pending_ts
    now = time.time()
    files = sorted(INBOX.glob("*.json"))

    for f in files:
        try:
            event = json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:
            log(f"inbox corrupt file {f.name}: {e}")
            try:
                f.rename(PROCESSED / f"bad_{f.name}")
            except Exception:
                pass
            continue

        ev_name = event.get("hook_event_name", "")
        user_active = is_user_active()
        # When the user is away, even Notifications are buffered
        # (avoids buzzing their phone at 4am).
        if ev_name in URGENT_EVENTS and not user_active:
            ev_name_effective = "_DEFER_" + ev_name
        else:
            ev_name_effective = ev_name

        if ev_name in BUFFERED_EVENTS or ev_name_effective.startswith("_DEFER_"):
            dst = PENDING / f.name
            try:
                f.rename(dst)
            except Exception as e:
                log(f"move-to-pending failed: {e}")
                continue
            event["_src"] = str(dst)
            if not pending_events:
                first_pending_ts = now
            pending_events.append(event)
            last_pending_ts = now
            deferred = " [defer-Notif]" if ev_name_effective.startswith("_DEFER_") else ""
            log(f"buffered ({len(pending_events)}){deferred}: {f.name}")
        elif ev_name in URGENT_EVENTS:
            msg = transform_notification(event)
            send_tg(msg)
            record_history(
                "event",
                f"{ev_name}@{event.get('project','?')}: {msg[:100]}",
            )
            try:
                f.rename(PROCESSED / f.name)
            except Exception:
                pass
            log(f"urgent sent: {f.name}")
        else:
            msg = transform_notification(event)
            send_tg(msg)
            try:
                f.rename(PROCESSED / f.name)
            except Exception:
                pass
            log(f"direct sent ({ev_name}): {f.name}")

    # Out-of-band flush trigger: `touch $HERALD_HOME/flush.trigger`
    trigger_file = HERALD_HOME / "flush.trigger"
    force_flush = False
    if trigger_file.exists():
        log("flush.trigger detected — forcing batch")
        try:
            trigger_file.unlink()
        except Exception:
            pass
        force_flush = True

    if pending_events:
        nowt = time.time()
        since_last = nowt - last_pending_ts
        since_first = nowt - first_pending_ts if first_pending_ts > 0 else 0
        count = len(pending_events)
        max_wait = (
            MAX_BATCH_WAIT_ACTIVE if is_user_active() else MAX_BATCH_WAIT_INACTIVE
        )
        reason = None
        if force_flush:
            reason = "trigger"
        elif since_last >= current_debounce():
            reason = "debounce"
        elif since_first >= max_wait:
            reason = (
                f"max_wait({int(since_first)}s, "
                f"{'active' if is_user_active() else 'inactive'})"
            )
        elif count >= MAX_PENDING_COUNT:
            reason = f"max_count({count})"
        if reason:
            if reason != "debounce":
                log(f"batch triggered: {reason}")
            summarize_and_send()


def poll_tg(state):
    r = tg_request(
        "getUpdates",
        {
            "offset": state["tg_update_offset"],
            "timeout": 20,
            "allowed_updates": '["message"]',
        },
        timeout=35,
    )
    if not r or not r.get("ok"):
        return
    for update in r.get("result", []):
        state["tg_update_offset"] = update["update_id"] + 1
        if "message" not in update:
            continue
        msg = update["message"]
        if msg.get("chat", {}).get("id") != int(CHAT_ID):
            log(f"dropped non-whitelisted chat_id={msg.get('chat',{}).get('id')}")
            continue
        try:
            handle_tg_message(msg)
        except Exception as e:
            log(f"handle_tg_message error: {e}")
            send_tg(f"({BOT_NAME} hiccuped: {e})")
    save_state(state)


def restore_pending():
    """Pick back up events that were in pending/ when the daemon last died."""
    global last_pending_ts, first_pending_ts
    if not PENDING.exists():
        return
    for f in sorted(PENDING.glob("*.json")):
        try:
            event = json.loads(f.read_text(encoding="utf-8"))
            event["_src"] = str(f)
            pending_events.append(event)
            ts = event.get("timestamp", 0)
            if ts > last_pending_ts:
                last_pending_ts = ts
            if first_pending_ts == 0 or (ts > 0 and ts < first_pending_ts):
                first_pending_ts = ts
        except Exception as e:
            log(f"restore corrupt {f.name}: {e}")
    if pending_events:
        log(
            f"restored pending: {len(pending_events)} "
            f"(first={int(first_pending_ts)}, last={int(last_pending_ts)})"
        )


def main():
    global debounce_level, last_batch_sent_ts, last_user_msg_ts, silence_until

    HERALD_HOME.mkdir(parents=True, exist_ok=True)
    for d in (INBOX, PENDING, PROCESSED):
        d.mkdir(parents=True, exist_ok=True)
    PIDFILE.write_text(str(os.getpid()))

    state = load_state()
    debounce_level = int(state.get("debounce_level", 0))
    last_batch_sent_ts = float(state.get("last_batch_sent_ts", 0.0))
    last_user_msg_ts = float(state.get("last_user_msg_ts", 0.0))
    silence_until = float(state.get("silence_until", 0.0))
    if silence_until > 0 and silence_until > time.time():
        log(f"restored silence: {int(silence_until - time.time())}s left")
    elif silence_until > 0:
        silence_until = 0.0
    if debounce_level > 0 or last_batch_sent_ts > 0:
        log(f"restored debounce: level={debounce_level}, last_batch={int(last_batch_sent_ts)}")
    if last_user_msg_ts > 0:
        mins_ago = int((time.time() - last_user_msg_ts) / 60)
        log(f"restored last_user_msg: {mins_ago}min ago (active={is_user_active()})")

    load_history()
    # Fallback: if state lacks last_user_msg_ts but history has a recent user turn.
    if last_user_msg_ts == 0 and history:
        for h in reversed(history):
            if h.get("role") == "user":
                last_user_msg_ts = float(h.get("ts", 0))
                mins = int((time.time() - last_user_msg_ts) / 60)
                log(
                    f"back-filled last_user_msg from history: "
                    f"{mins}min ago (active={is_user_active()})"
                )
                break
    restore_pending()

    log(f"daemon up (model={MODEL}, debounce_base={DEBOUNCE_LEVELS[0]//60}min)")

    # Greeting: let the persona improvise one line. No template.
    try:
        greeting = call_claude(
            f"{PERSONA}\n\n---\n\n"
            f"You just woke up (daemon process just started). "
            f"Current time is {time.strftime('%H:%M')}, there are "
            f"{len(pending_events)} buffered events pending summary. "
            f"In your voice, send a very brief greeting to {USER_NAME} — "
            "improvise based on the hour and mood. No fixed phrasing. "
            "Avoid technical words like 'online' / 'startup'. One line, ~15 words max."
        )
        if greeting and not greeting.startswith("("):
            send_tg(greeting)
            record_history("assistant", greeting)
            log(f"greeting: {greeting[:100]}")
        else:
            send_tg("…")
            log(f"greeting fallback (gen failed): {greeting[:100] if greeting else 'empty'}")
    except Exception as e:
        log(f"greeting error: {e}")
        send_tg("…")

    while True:
        try:
            poll_inbox()
            poll_tg(state)
        except Exception as e:
            log(f"main loop exception: {e}")
            time.sleep(5)
        time.sleep(1)


if __name__ == "__main__":
    main()

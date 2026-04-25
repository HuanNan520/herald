#!/usr/bin/env python3
"""Claude Code hook entry point.

Dumps the hook's event JSON into `$HERALD_HOME/inbox/` so the daemon can pick
it up. If the daemon is down, events simply accumulate on disk and get
delivered whenever it comes back.

Kept intentionally tiny: runs synchronously as part of every Stop/Notification
hook, so any bug here would degrade the user's Claude Code experience.
"""
import json
import os
import sys
import time
import uuid
from pathlib import Path

# The daemon sets HERALD_SILENCE=1 in the env of every `claude -p` it spawns.
# Without this escape hatch, a sub-Claude triggers the Stop hook → writes to
# inbox → daemon reads it → spawns another sub-Claude → ... (recursive LLM fork bomb).
if os.environ.get("HERALD_SILENCE") == "1":
    sys.exit(0)

HERALD_HOME = Path(
    os.environ.get("HERALD_HOME", str(Path.home() / ".herald"))
).expanduser()
INBOX = HERALD_HOME / "inbox"

try:
    INBOX.mkdir(parents=True, exist_ok=True)
except Exception:
    sys.exit(0)

try:
    raw = sys.stdin.read() or "{}"
    data = json.loads(raw)
except json.JSONDecodeError:
    data = {}

event = {
    "hook_event_name": data.get("hook_event_name", "Unknown"),
    "project": Path(data.get("cwd", "") or "").name or "unknown",
    "session": (data.get("session_id", "") or "")[:6],
    "cwd": data.get("cwd", ""),
    "transcript_path": data.get("transcript_path", ""),
    "timestamp": time.time(),
}
if data.get("message"):
    event["message"] = data["message"]

fname = INBOX / f"{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}.json"
try:
    fname.write_text(json.dumps(event, ensure_ascii=False), encoding="utf-8")
except Exception:
    pass

sys.exit(0)

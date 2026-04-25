#!/bin/bash
# Loads .env, resolves paths, execs the daemon.
set -euo pipefail

# Resolve repo root based on script location.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"

# Load .env from repo root if present.
if [ -f "$REPO/.env" ]; then
    set -a
    . "$REPO/.env"
    set +a
fi

# Default HERALD_HOME to the repo dir if not set — handy for a single-user
# personal deployment where you don't want runtime state outside the clone.
: "${HERALD_HOME:=$HOME/.herald}"
: "${PERSONA_FILE:=$HERALD_HOME/persona.txt}"
: "${PROJECTS_FILE:=$HERALD_HOME/projects.json}"
export HERALD_HOME PERSONA_FILE PROJECTS_FILE

# If user kept config in the repo (persona.txt, projects.json in repo root),
# point the daemon at those instead of HERALD_HOME copies.
[ -f "$REPO/persona.txt" ]   && export PERSONA_FILE="$REPO/persona.txt"
[ -f "$REPO/projects.json" ] && export PROJECTS_FILE="$REPO/projects.json"

mkdir -p "$HERALD_HOME"
cd "$REPO"
exec python3 "$REPO/daemon.py"

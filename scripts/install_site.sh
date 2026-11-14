#!/bin/bash
# Copy the static dashboard (web/) into the Vercel deploy directory (keeps its .vercel project link).
# Live data is NOT copied: the dashboard reads bot.json from the GitHub `data` branch.
set -e
SRC="$(cd "$(dirname "$0")/../web" && pwd)"
DST="${1:-${TBOT_SITE:-/workspace/ultimate/frontend}}"
mkdir -p "$DST"
cp -f "$SRC"/* "$DST"/
echo "site installed into $DST"

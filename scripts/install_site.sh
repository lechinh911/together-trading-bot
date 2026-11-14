#!/bin/bash
# Copy the static dashboard (web/) into the deploy directory. Keeps .vercel (project link) and data/bot.json.
set -e
SRC="$(cd "$(dirname "$0")/../web" && pwd)"
DST="${1:-${TBOT_SITE:-/workspace/ultimate/frontend}}"
mkdir -p "$DST/data"
cp -f "$SRC"/* "$DST"/
echo "site installed into $DST"

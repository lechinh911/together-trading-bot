#!/bin/bash
# Start the bot in the foreground (the process manager runs this). Signal-only, no API keys needed.
cd "$(dirname "$0")/.." || exit 1
exec .venv/bin/python -m tbot

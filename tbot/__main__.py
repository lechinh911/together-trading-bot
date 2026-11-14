import asyncio
import logging
import logging.handlers
import os
import sys

from .bot import Bot
from .config import Settings


def main():
    s = Settings()
    os.makedirs(os.path.join(s.data_dir, "logs"), exist_ok=True)
    h = logging.handlers.RotatingFileHandler(os.path.join(s.data_dir, "logs", "bot.log"), maxBytes=5_000_000, backupCount=3)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    h.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(h)
    if sys.stdout.isatty():
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    asyncio.run(Bot(s).run())


if __name__ == "__main__":
    main()

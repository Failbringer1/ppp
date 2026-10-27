"""python -m tgads — start the panel and background workers."""
from __future__ import annotations

import logging

import uvicorn

from .config import get_config
from .web.app import create_app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("telethon").setLevel(logging.WARNING)
    cfg = get_config()
    uvicorn.run(create_app(), host=cfg.host, port=cfg.port, log_level="info")


if __name__ == "__main__":
    main()

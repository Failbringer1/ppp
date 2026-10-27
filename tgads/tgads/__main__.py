"""python -m tgads — start the panel and background workers."""
from __future__ import annotations

import logging
import logging.handlers

import uvicorn

from .config import get_config
from .web.app import create_app


def main() -> None:
    cfg = get_config()
    handlers = None
    if cfg.log_file:
        handlers = [logging.handlers.RotatingFileHandler(cfg.log_file, maxBytes=10 * 1024 * 1024, backupCount=3,
                                                         encoding="utf-8")]
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=handlers)
    logging.getLogger("telethon").setLevel(logging.WARNING)
    # with a log file uvicorn's own loggers propagate to it instead of printing to the console
    uvicorn.run(create_app(), host=cfg.host, port=cfg.port, log_level="info",
                log_config=None if cfg.log_file else uvicorn.config.LOGGING_CONFIG)


if __name__ == "__main__":
    main()

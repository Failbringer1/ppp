"""Process configuration from environment / .env."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Config(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="TGADS_", extra="ignore")

    data_dir: Path = Path("data")
    host: str = "127.0.0.1"
    port: int = 8080
    # HTTP basic auth for the panel; strongly recommended on a VPS
    panel_user: str = "admin"
    panel_password: str = ""

    # https://my.telegram.org -> API development tools
    tg_api_id: int = 0
    tg_api_hash: str = ""

    # claude_cli | api | manual | auto (api if key set, else claude_cli if installed, else manual)
    ai_mode: str = "auto"
    claude_bin: str = "claude"
    claude_model: str = "opus"           # alias for the CLI
    claude_config_dir: str = ""          # CLAUDE_CONFIG_DIR for the CLI when run as a service
    claude_oauth_token: str = ""         # from `claude setup-token`; can also be saved in the panel settings
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-opus-5-5"

    # TGStat scraping through a headless browser when Cloudflare blocks plain requests: auto | 1 | 0
    tgstat_browser: str = "auto"
    run_worker: bool = True
    log_file: str = ""                   # rotating log file instead of the console (Windows service)

    @property
    def db_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.data_dir / 'tgads.db'}"

    @property
    def sessions_dir(self) -> Path:
        return self.data_dir / "sessions"

    @property
    def media_dir(self) -> Path:
        return self.data_dir / "media"


@lru_cache
def get_config() -> Config:
    cfg = Config()
    for d in (cfg.data_dir, cfg.sessions_dir, cfg.media_dir):
        d.mkdir(parents=True, exist_ok=True)
    return cfg

"""
Central configuration. Loads from .env via pydantic-settings.
Nothing secret is hardcoded here -- everything comes from environment.
"""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Telegram
    BOT_TOKEN: str

    # Database
    DATABASE_URL: str = "sqlite+aiosqlite:///./data/bot.db"

    # Solana
    SOLANA_RPC_URL: str = "https://api.mainnet-beta.solana.com"
    SOLANA_WS_URL: str = "wss://api.mainnet-beta.solana.com"
    JUPITER_QUOTE_API: str = "https://quote-api.jup.ag/v6"
    HELIUS_API_KEY: str = ""

    # TON
    TON_API_KEY: str = ""
    TON_API_BASE: str = "https://toncenter.com/api/v2"
    TONAPI_BASE: str = "https://tonapi.io"

    # Security
    MASTER_PEPPER: str

    # Ops
    LOG_LEVEL: str = "INFO"
    ENVIRONMENT: str = "development"
    ADMIN_TELEGRAM_IDS: str = ""

    # Fees
    PLATFORM_FEE_BPS: int = 0
    FEE_WALLET_SOL: str = ""
    FEE_WALLET_TON: str = ""

    @property
    def admin_ids(self) -> set[int]:
        return {int(x) for x in self.ADMIN_TELEGRAM_IDS.split(",") if x.strip()}


settings = Settings()

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

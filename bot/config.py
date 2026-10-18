import logging
import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(frozen=True)
class BotConfig:
    bot_token: str
    owner_id: int
    database_url: str
    log_level: int


def load_config() -> BotConfig:
    load_dotenv()

    bot_token = os.getenv("BOT_TOKEN", "").strip()
    database_url = os.getenv("DATABASE_URL", "").strip()
    owner_id_value = os.getenv("OWNER_ID", "").strip()
    if not bot_token:
        raise ValueError("BOT_TOKEN must be set.")
    if not database_url:
        raise ValueError("DATABASE_URL must be set.")
    if not owner_id_value:
        raise ValueError("OWNER_ID must be set.")
    try:
        owner_id = int(owner_id_value)
    except ValueError as exc:
        raise ValueError("OWNER_ID must be a Telegram user ID.") from exc
    if owner_id <= 0:
        raise ValueError("OWNER_ID must be a positive Telegram user ID.")

    log_level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    log_level = getattr(logging, log_level_name, None)
    if not isinstance(log_level, int):
        raise ValueError(f"LOG_LEVEL is not a valid logging level: {log_level_name}")

    return BotConfig(
        bot_token=bot_token,
        owner_id=owner_id,
        database_url=database_url,
        log_level=log_level,
    )
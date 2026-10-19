from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import asyncpg


@dataclass(frozen=True)
class BotSettings:
    destination_chat_id: str | None = None
    topic_id: int | None = None
    caption_mode: str = "original"
    caption_text: str | None = None
    caption_search: str | None = None
    caption_replacement: str | None = None


def _asyncpg_dsn(database_url: str) -> str:
    parts = urlsplit(database_url)
    scheme = "postgresql" if parts.scheme == "postgres" else parts.scheme
    query = parse_qsl(parts.query, keep_blank_values=True)
    sslmode = next((value for key, value in query if key == "sslmode"), None)
    query = [(key, value) for key, value in query if key != "sslmode"]
    if sslmode and sslmode not in {"disable", "allow", "prefer"}:
        query.append(("ssl", "require" if sslmode == "require" else sslmode))
    return urlunsplit((scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


class Database:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    @classmethod
    async def connect(cls, database_url: str) -> "Database":
        pool = await asyncpg.create_pool(_asyncpg_dsn(database_url), min_size=1, max_size=5)
        database = cls(pool)
        try:
            await database.initialize()
        except Exception:
            await pool.close()
            raise
        return database

    async def initialize(self) -> None:
        async with self._pool.acquire() as connection:
            await connection.execute(
                """
                CREATE TABLE IF NOT EXISTS bot_settings (
                    singleton_id SMALLINT PRIMARY KEY CHECK (singleton_id = 1),
                    destination_chat_id TEXT,
                    topic_id BIGINT,
                    caption_mode TEXT NOT NULL DEFAULT 'original'
                        CONSTRAINT bot_settings_caption_mode_check
                        CHECK (caption_mode IN (
                            'original', 'append', 'prepend',
                            'replace_parcial', 'replace_total'
                        )),
                    caption_text TEXT,
                    caption_search TEXT,
                    caption_replacement TEXT,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            await connection.execute(
                """
                ALTER TABLE bot_settings
                    DROP CONSTRAINT IF EXISTS bot_settings_caption_mode_check;
                UPDATE bot_settings
                SET caption_mode = 'replace_parcial'
                WHERE caption_mode = 'replace';
                ALTER TABLE bot_settings
                    ADD CONSTRAINT bot_settings_caption_mode_check
                    CHECK (caption_mode IN (
                        'original', 'append', 'prepend',
                        'replace_parcial', 'replace_total'
                    ));
                """
            )
            await connection.execute(
                "INSERT INTO bot_settings (singleton_id) VALUES (1) ON CONFLICT DO NOTHING"
            )

    async def get_settings(self) -> BotSettings:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT destination_chat_id, topic_id, caption_mode,
                       caption_text, caption_search, caption_replacement
                FROM bot_settings
                WHERE singleton_id = 1
                """
            )
        if row is None:
            raise RuntimeError("The bot_settings row was not initialized.")
        return BotSettings(
            destination_chat_id=row["destination_chat_id"],
            topic_id=row["topic_id"],
            caption_mode=row["caption_mode"],
            caption_text=row["caption_text"],
            caption_search=row["caption_search"],
            caption_replacement=row["caption_replacement"],
        )

    async def set_destination(self, chat_id: str) -> None:
        async with self._pool.acquire() as connection:
            await connection.execute(
                """
                UPDATE bot_settings
                SET destination_chat_id = $1, topic_id = NULL, updated_at = NOW()
                WHERE singleton_id = 1
                """,
                chat_id,
            )

    async def set_topic(self, topic_id: int | None) -> None:
        async with self._pool.acquire() as connection:
            await connection.execute(
                """
                UPDATE bot_settings
                SET topic_id = $1, updated_at = NOW()
                WHERE singleton_id = 1
                """,
                topic_id,
            )

    async def set_caption(
        self,
        mode: str,
        *,
        text: str | None = None,
        search: str | None = None,
        replacement: str | None = None,
    ) -> None:
        async with self._pool.acquire() as connection:
            await connection.execute(
                """
                UPDATE bot_settings
                SET caption_mode = $1, caption_text = $2, caption_search = $3,
                    caption_replacement = $4, updated_at = NOW()
                WHERE singleton_id = 1
                """,
                mode,
                text,
                search,
                replacement,
            )

    async def close(self) -> None:
        await self._pool.close()
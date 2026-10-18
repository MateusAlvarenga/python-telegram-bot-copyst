import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock
from types import SimpleNamespace

from telegram import BotCommandScopeChat, Chat, Message, MessageEntity, PhotoSize, Update, User

from bot.config import BotConfig
from bot.db import BotSettings, Database, _asyncpg_dsn
from bot.handlers import _is_owner, _media_input, transform_caption
from bot.main import OWNER_COMMANDS, register_owner_commands


class CaptionTransformationTests(unittest.TestCase):
    def test_original_caption_and_entities_are_unchanged(self) -> None:
        entity = MessageEntity(MessageEntity.BOLD, offset=0, length=4)
        original, entities, should_override = transform_caption(
            "text",
            [entity],
            BotSettings(),
        )

        self.assertEqual(original, "text")
        self.assertEqual(entities, [entity])
        self.assertFalse(should_override)

    def test_append_uses_newline_and_preserves_caption_entities(self) -> None:
        entity = MessageEntity(MessageEntity.BOLD, offset=0, length=4)
        transformed, entities, should_override = transform_caption(
            "text",
            [entity],
            BotSettings(caption_mode="append", caption_text="more"),
        )

        self.assertEqual(transformed, "text\nmore")
        self.assertEqual(entities, [entity])
        self.assertTrue(should_override)

    def test_prepend_shifts_entities_by_utf16_length(self) -> None:
        entity = MessageEntity(MessageEntity.BOLD, offset=0, length=4)
        transformed, entities, should_override = transform_caption(
            "text",
            [entity],
            BotSettings(caption_mode="prepend", caption_text="🚀"),
        )

        self.assertEqual(transformed, "🚀\ntext")
        self.assertIsNot(entities[0], entity)
        self.assertEqual(entities[0].offset, 3)
        self.assertTrue(should_override)

    def test_replace_changes_every_case_sensitive_occurrence(self) -> None:
        transformed, entities, should_override = transform_caption(
            "Text text text",
            None,
            BotSettings(
                caption_mode="replace",
                caption_search="text",
                caption_replacement="media",
            ),
        )

        self.assertEqual(transformed, "Text media media")
        self.assertIsNone(entities)
        self.assertTrue(should_override)

    def test_replace_with_empty_text_removes_matches(self) -> None:
        transformed, _, should_override = transform_caption(
            "remove this phrase",
            None,
            BotSettings(
                caption_mode="replace",
                caption_search="this ",
                caption_replacement="",
            ),
        )

        self.assertEqual(transformed, "remove phrase")
        self.assertTrue(should_override)

    def test_replace_with_no_match_leaves_original_untouched(self) -> None:
        transformed, _, should_override = transform_caption(
            "unchanged",
            None,
            BotSettings(
                caption_mode="replace",
                caption_search="missing",
                caption_replacement="new",
            ),
        )

        self.assertEqual(transformed, "unchanged")
        self.assertFalse(should_override)

    def test_album_photo_input_retains_spoiler_and_transforms_caption(self) -> None:
        message = Message(
            message_id=7,
            date=datetime.now(timezone.utc),
            chat=Chat(id=123, type=Chat.PRIVATE),
            photo=[PhotoSize("file-id", "unique-id", width=100, height=100)],
            caption="original",
            media_group_id="album",
            has_media_spoiler=True,
        )

        media = _media_input(
            message,
            BotSettings(caption_mode="append", caption_text="additional"),
            transform=True,
        )

        self.assertEqual(media.caption, "original\nadditional")
        self.assertTrue(media.has_spoiler)


class DatabaseUrlTests(unittest.TestCase):
    def test_normalizes_railway_postgres_scheme(self) -> None:
        self.assertEqual(
            _asyncpg_dsn("postgres://user:pass@host:5432/db"),
            "postgresql://user:pass@host:5432/db",
        )

    def test_converts_sslmode_query_for_asyncpg(self) -> None:
        self.assertEqual(
            _asyncpg_dsn("postgresql://user:pass@host/db?sslmode=require"),
            "postgresql://user:pass@host/db?ssl=require",
        )


class FakeConnection:
    def __init__(self) -> None:
        self.executions: list[tuple[str, tuple[object, ...]]] = []

    async def execute(self, query: str, *args: object) -> str:
        self.executions.append((query, args))
        return "OK"

    async def fetchrow(self, query: str) -> dict[str, object]:
        return {
            "destination_chat_id": "-100123",
            "topic_id": 45,
            "caption_mode": "replace",
            "caption_text": None,
            "caption_search": "old",
            "caption_replacement": "new",
        }


class FakePool:
    def __init__(self) -> None:
        self.connection = FakeConnection()

    @asynccontextmanager
    async def acquire(self):
        yield self.connection


class DatabaseSettingsTests(unittest.IsolatedAsyncioTestCase):
    async def test_reads_persisted_settings_and_applies_updates(self) -> None:
        pool = FakePool()
        database = Database(pool)

        settings = await database.get_settings()
        await database.set_destination("-100456")
        await database.set_topic(72)
        await database.set_caption("append", text="new caption")

        self.assertEqual(settings.destination_chat_id, "-100123")
        self.assertEqual(settings.topic_id, 45)
        self.assertEqual(settings.caption_search, "old")
        self.assertEqual(pool.connection.executions[0][1], ("-100456",))
        self.assertEqual(pool.connection.executions[1][1], (72,))
        self.assertEqual(
            pool.connection.executions[2][1],
            ("append", "new caption", None, None),
        )


class OwnerAccessTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_owner_in_private_chat_is_authorized(self) -> None:
        owner = User(id=123, is_bot=False, first_name="owner")
        config = BotConfig("token", 123, "postgresql://localhost/db", 20)
        context = SimpleNamespace(
            application=SimpleNamespace(bot_data={"config": config}),
        )

        def make_update(user: User, chat: Chat) -> Update:
            message = Message(
                message_id=1,
                date=datetime.now(timezone.utc),
                chat=chat,
                from_user=user,
            )
            return Update(update_id=1, message=message)

        private_owner = make_update(owner, Chat(id=123, type=Chat.PRIVATE))
        other_user = make_update(User(id=456, is_bot=False, first_name="other"), Chat(
            id=456,
            type=Chat.PRIVATE,
        ))
        owner_in_group = make_update(
            owner,
            Chat(id=-100123, type=Chat.SUPERGROUP),
        )

        self.assertTrue(await _is_owner(private_owner, context))
        self.assertFalse(await _is_owner(other_user, context))
        self.assertFalse(await _is_owner(owner_in_group, context))


class OwnerCommandMenuTests(unittest.IsolatedAsyncioTestCase):
    async def test_registers_commands_only_in_owner_private_chat_scope(self) -> None:
        bot = SimpleNamespace(set_my_commands=AsyncMock())

        await register_owner_commands(bot, owner_id=123)

        bot.set_my_commands.assert_awaited_once()
        commands = bot.set_my_commands.await_args.args[0]
        scope = bot.set_my_commands.await_args.kwargs["scope"]
        self.assertEqual(commands, OWNER_COMMANDS)
        self.assertEqual(scope, BotCommandScopeChat(chat_id=123))


if __name__ == "__main__":
    unittest.main()

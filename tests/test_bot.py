import asyncio
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, call, patch

from telegram import (
    BotCommandScopeChat,
    Chat,
    InputMediaVideo,
    Message,
    MessageEntity,
    PhotoSize,
    Update,
    User,
    Video,
)
from telegram.error import RetryAfter, TimedOut
from telegram.ext import CommandHandler

from bot.config import BotConfig
from bot.db import BotSettings, Database, _asyncpg_dsn
from bot.handlers import (
    DeliveryJob,
    MAX_TIMEOUT_ATTEMPTS,
    _call_with_retries,
    _copy_album,
    _delivery_worker,
    _enqueue_delivery,
    _is_owner,
    _media_input,
    about,
    caption,
    list_destination_aliases,
    load_destination_alias,
    save_destination_alias,
    transform_caption,
)
from bot.main import (
    BOT_API_TIMEOUT_SECONDS,
    MEDIA_WRITE_TIMEOUT_SECONDS,
    OWNER_COMMANDS,
    POLLING_READ_TIMEOUT_SECONDS,
    create_application,
    register_owner_commands,
)


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

    def test_replace_parcial_changes_every_case_sensitive_occurrence(self) -> None:
        transformed, entities, should_override = transform_caption(
            "Text text text",
            None,
            BotSettings(
                caption_mode="replace_parcial",
                caption_search="text",
                caption_replacement="media",
            ),
        )

        self.assertEqual(transformed, "Text media media")
        self.assertIsNone(entities)
        self.assertTrue(should_override)

    def test_replace_parcial_with_empty_text_removes_matches(self) -> None:
        transformed, _, should_override = transform_caption(
            "remove this phrase",
            None,
            BotSettings(
                caption_mode="replace_parcial",
                caption_search="this ",
                caption_replacement="",
            ),
        )

        self.assertEqual(transformed, "remove phrase")
        self.assertTrue(should_override)

    def test_replace_parcial_with_no_match_leaves_original_untouched(self) -> None:
        transformed, _, should_override = transform_caption(
            "unchanged",
            None,
            BotSettings(
                caption_mode="replace_parcial",
                caption_search="missing",
                caption_replacement="new",
            ),
        )

        self.assertEqual(transformed, "unchanged")
        self.assertFalse(should_override)

    def test_replace_total_replaces_caption_and_removes_entities(self) -> None:
        entity = MessageEntity(MessageEntity.BOLD, offset=0, length=4)

        transformed, entities, should_override = transform_caption(
            "original text",
            [entity],
            BotSettings(caption_mode="replace_total", caption_text="new caption"),
        )

        self.assertEqual(transformed, "new caption")
        self.assertIsNone(entities)
        self.assertTrue(should_override)

    def test_replace_total_adds_caption_when_original_is_missing(self) -> None:
        transformed, entities, should_override = transform_caption(
            None,
            None,
            BotSettings(caption_mode="replace_total", caption_text="new caption"),
        )

        self.assertEqual(transformed, "new caption")
        self.assertIsNone(entities)
        self.assertTrue(should_override)

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

    def test_album_video_input_does_not_require_incoming_supports_streaming(self) -> None:
        message = Message(
            message_id=8,
            date=datetime.now(timezone.utc),
            chat=Chat(id=123, type=Chat.PRIVATE),
            video=Video("file-id", "unique-id", width=640, height=360, duration=9),
            media_group_id="album",
        )

        media = _media_input(message, BotSettings(), transform=False)

        self.assertIsInstance(media, InputMediaVideo)
        self.assertEqual(media.media, "file-id")


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
        self.fetches: list[tuple[str, tuple[object, ...]]] = []
        self.alias_row: dict[str, object] | None = None

    async def execute(self, query: str, *args: object) -> str:
        self.executions.append((query, args))
        return "OK"

    async def fetchrow(self, query: str, *args: object) -> dict[str, object] | None:
        if "FROM destination_aliases" in query:
            return self.alias_row
        return {
            "destination_chat_id": "-100123",
            "topic_id": 45,
            "caption_mode": "replace_parcial",
            "caption_text": None,
            "caption_search": "old",
            "caption_replacement": "new",
        }

    async def fetch(self, query: str, *args: object) -> list[dict[str, object]]:
        self.fetches.append((query, args))
        return [
            {
                "alias": "geral",
                "destination_chat_id": "-100456",
                "topic_id": None,
            },
            {
                "alias": "novidades",
                "destination_chat_id": "-100789",
                "topic_id": 91,
            },
        ]


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
        await database.set_destination_and_topic("-100789", 91)

        self.assertEqual(settings.destination_chat_id, "-100123")
        self.assertEqual(settings.topic_id, 45)
        self.assertEqual(settings.caption_search, "old")
        self.assertEqual(pool.connection.executions[0][1], ("-100456",))
        self.assertEqual(pool.connection.executions[1][1], (72,))
        self.assertEqual(
            pool.connection.executions[2][1],
            ("append", "new caption", None, None),
        )
        self.assertEqual(pool.connection.executions[3][1], ("-100789", 91))

    async def test_initialization_migrates_old_replace_mode(self) -> None:
        pool = FakePool()
        database = Database(pool)

        await database.initialize()

        self.assertTrue(
            any(
                "CREATE TABLE IF NOT EXISTS destination_aliases" in query
                for query, _ in pool.connection.executions
            )
        )
        migration = next(
            query
            for query, _ in pool.connection.executions
            if "DROP CONSTRAINT IF EXISTS bot_settings_caption_mode_check" in query
        )
        self.assertIn("DROP CONSTRAINT IF EXISTS bot_settings_caption_mode_check", migration)
        self.assertIn("SET caption_mode = 'replace_parcial'", migration)
        self.assertIn("'replace_total'", migration)

    async def test_saves_alias_with_upsert_and_reads_destination_and_topic(self) -> None:
        pool = FakePool()
        database = Database(pool)
        pool.connection.alias_row = {
            "destination_chat_id": "-100789",
            "topic_id": 91,
        }

        await database.save_destination_alias("novidades", "-100789", 91)
        settings = await database.get_destination_alias("novidades")

        self.assertEqual(
            pool.connection.executions[0][1],
            ("novidades", "-100789", 91),
        )
        self.assertIn("ON CONFLICT (alias) DO UPDATE", pool.connection.executions[0][0])
        self.assertEqual(settings, ("-100789", 91))

    async def test_returns_none_for_unknown_alias(self) -> None:
        database = Database(FakePool())

        self.assertIsNone(await database.get_destination_alias("ausente"))

    async def test_lists_aliases_ordered_by_name(self) -> None:
        pool = FakePool()
        database = Database(pool)

        aliases = await database.list_destination_aliases()

        self.assertIn("ORDER BY alias", pool.connection.fetches[-1][0])
        self.assertEqual(
            aliases,
            [
                ("geral", "-100456", None),
                ("novidades", "-100789", 91),
            ],
        )


class DestinationAliasCommandTests(unittest.IsolatedAsyncioTestCase):
    def make_update_and_context(
        self,
        database: SimpleNamespace,
        args: list[str],
    ) -> tuple[SimpleNamespace, SimpleNamespace, AsyncMock]:
        reply_text = AsyncMock()
        update = SimpleNamespace(
            effective_user=User(id=123, is_bot=False, first_name="owner"),
            effective_chat=Chat(id=123, type=Chat.PRIVATE),
            effective_message=SimpleNamespace(reply_text=reply_text),
        )
        context = SimpleNamespace(
            args=args,
            application=SimpleNamespace(
                bot_data={
                    "config": BotConfig("token", 123, "postgresql://localhost/db", 20),
                    "database": database,
                },
            ),
        )
        return update, context, reply_text

    async def test_save_command_persists_current_destination_and_topic(self) -> None:
        database = SimpleNamespace(
            get_settings=AsyncMock(
                return_value=BotSettings(
                    destination_chat_id="-100789",
                    topic_id=91,
                ),
            ),
            save_destination_alias=AsyncMock(),
        )
        update, context, reply_text = self.make_update_and_context(
            database,
            ["novidades"],
        )

        await save_destination_alias(update, context)

        database.save_destination_alias.assert_awaited_once_with(
            "novidades",
            "-100789",
            91,
        )
        reply_text.assert_awaited_once()

    async def test_save_command_requires_a_configured_destination(self) -> None:
        database = SimpleNamespace(
            get_settings=AsyncMock(return_value=BotSettings()),
            save_destination_alias=AsyncMock(),
        )
        update, context, reply_text = self.make_update_and_context(
            database,
            ["novidades"],
        )

        await save_destination_alias(update, context)

        database.save_destination_alias.assert_not_awaited()
        reply_text.assert_awaited_once()

    async def test_load_command_restores_destination_and_topic_together(self) -> None:
        database = SimpleNamespace(
            get_destination_alias=AsyncMock(return_value=("-100789", 91)),
            set_destination_and_topic=AsyncMock(),
        )
        update, context, reply_text = self.make_update_and_context(
            database,
            ["novidades"],
        )

        await load_destination_alias(update, context)

        database.set_destination_and_topic.assert_awaited_once_with("-100789", 91)
        reply_text.assert_awaited_once()

    async def test_load_command_does_not_change_settings_for_unknown_alias(self) -> None:
        database = SimpleNamespace(
            get_destination_alias=AsyncMock(return_value=None),
            set_destination_and_topic=AsyncMock(),
        )
        update, context, reply_text = self.make_update_and_context(
            database,
            ["ausente"],
        )

        await load_destination_alias(update, context)

        database.set_destination_and_topic.assert_not_awaited()
        reply_text.assert_awaited_once()

    async def test_list_command_displays_alias_destination_and_topic_table(self) -> None:
        database = SimpleNamespace(
            list_destination_aliases=AsyncMock(
                return_value=[
                    ("novidades", "-100789", 91),
                    ("geral", "-100456", None),
                ],
            ),
        )
        update, context, reply_text = self.make_update_and_context(database, [])

        await list_destination_aliases(update, context)

        database.list_destination_aliases.assert_awaited_once_with()
        message, = reply_text.await_args.args
        self.assertIn("<pre>", message)
        self.assertIn("Apelido", message)
        self.assertIn("Destino", message)
        self.assertIn("Tópico", message)
        self.assertIn("novidades", message)
        self.assertIn("-100789", message)
        self.assertIn("91", message)
        self.assertIn("nenhum", message)
        self.assertEqual(reply_text.await_args.kwargs, {"parse_mode": "HTML"})

    async def test_list_command_reports_when_no_aliases_are_saved(self) -> None:
        database = SimpleNamespace(list_destination_aliases=AsyncMock(return_value=[]))
        update, context, reply_text = self.make_update_and_context(database, [])

        await list_destination_aliases(update, context)

        reply_text.assert_awaited_once_with("Nenhum apelido salvo.")


class CaptionCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_replace_total_command_saves_entire_caption(self) -> None:
        database = SimpleNamespace(set_caption=AsyncMock())
        reply_text = AsyncMock()
        update = SimpleNamespace(
            effective_user=User(id=123, is_bot=False, first_name="owner"),
            effective_chat=Chat(id=123, type=Chat.PRIVATE),
            effective_message=SimpleNamespace(
                text="/caption replace_total New caption",
                reply_text=reply_text,
            ),
        )
        context = SimpleNamespace(
            application=SimpleNamespace(
                bot_data={
                    "config": BotConfig("token", 123, "postgresql://localhost/db", 20),
                    "database": database,
                },
            ),
        )

        await caption(update, context)

        database.set_caption.assert_awaited_once_with(
            "replace_total",
            text="New caption",
        )
        reply_text.assert_awaited_once()

    async def test_replace_parcial_command_uses_search_and_replacement(self) -> None:
        database = SimpleNamespace(set_caption=AsyncMock())
        update = SimpleNamespace(
            effective_user=User(id=123, is_bot=False, first_name="owner"),
            effective_chat=Chat(id=123, type=Chat.PRIVATE),
            effective_message=SimpleNamespace(
                text="/caption replace_parcial old => new",
                reply_text=AsyncMock(),
            ),
        )
        context = SimpleNamespace(
            application=SimpleNamespace(
                bot_data={
                    "config": BotConfig("token", 123, "postgresql://localhost/db", 20),
                    "database": database,
                },
            ),
        )

        await caption(update, context)

        database.set_caption.assert_awaited_once_with(
            "replace_parcial",
            search="old",
            replacement="new",
        )


class AlbumCaptionTests(unittest.IsolatedAsyncioTestCase):
    async def test_replace_total_adds_caption_to_uncaptioned_album(self) -> None:
        chat = Chat(id=123, type=Chat.PRIVATE)
        date = datetime.now(timezone.utc)
        messages = [
            Message(
                message_id=message_id,
                date=date,
                chat=chat,
                photo=[PhotoSize(f"file-{message_id}", f"unique-{message_id}", 100, 100)],
                media_group_id="album",
            )
            for message_id in (2, 1)
        ]
        settings = BotSettings(
            destination_chat_id="-100456",
            caption_mode="replace_total",
            caption_text="new caption",
        )
        send_media_group = AsyncMock()
        context = SimpleNamespace(
            bot=SimpleNamespace(send_media_group=send_media_group),
        )

        await _copy_album(messages, settings, context)

        media = send_media_group.await_args.kwargs["media"]
        self.assertEqual(media[0].caption, "new caption")
        self.assertIsNone(media[1].caption)


class DeliveryRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_waits_for_retry_after_before_repeating_operation(self) -> None:
        operation = AsyncMock(side_effect=[RetryAfter(3), "sent"])

        with patch("bot.handlers.asyncio.sleep", new_callable=AsyncMock) as sleep:
            result = await _call_with_retries(operation)

        self.assertEqual(result, "sent")
        self.assertEqual(operation.await_count, 2)
        sleep.assert_awaited_once_with(3.0)

    async def test_retries_timeouts_with_exponential_backoff(self) -> None:
        operation = AsyncMock(
            side_effect=[TimedOut("network timeout"), TimedOut("network timeout"), "sent"]
        )

        with patch("bot.handlers.asyncio.sleep", new_callable=AsyncMock) as sleep:
            result = await _call_with_retries(operation)

        self.assertEqual(result, "sent")
        self.assertEqual(operation.await_count, 3)
        self.assertEqual(sleep.await_args_list, [call(1), call(2)])

    async def test_stops_after_five_timeout_attempts(self) -> None:
        operation = AsyncMock(side_effect=TimedOut("network timeout"))

        with (
            patch("bot.handlers.asyncio.sleep", new_callable=AsyncMock) as sleep,
            self.assertRaises(TimedOut),
        ):
            await _call_with_retries(operation)

        self.assertEqual(operation.await_count, MAX_TIMEOUT_ATTEMPTS)
        self.assertEqual(sleep.await_args_list, [call(1), call(2), call(4), call(8)])


class DeliveryQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_album_placeholder_preserves_fifo_order_for_following_messages(self) -> None:
        queue: asyncio.Queue[DeliveryJob] = asyncio.Queue()
        context = SimpleNamespace(
            application=SimpleNamespace(bot_data={"delivery_queue": queue}),
        )
        album_message = SimpleNamespace(message_id=1)
        following_message = SimpleNamespace(message_id=2)
        album_job = _enqueue_delivery([], context, wait_until_ready=True)
        _enqueue_delivery([following_message], context)
        delivered: list[int] = []

        async def record_delivery(job: DeliveryJob) -> None:
            delivered.append(job.messages[0].message_id)

        with patch("bot.handlers._deliver_messages", side_effect=record_delivery):
            worker = asyncio.create_task(
                _delivery_worker(context.application),
            )
            try:
                await asyncio.sleep(0)
                self.assertEqual(delivered, [])
                album_job.messages.append(album_message)
                album_job.ready.set()
                await asyncio.wait_for(queue.join(), timeout=1)
            finally:
                worker.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await worker

        self.assertEqual(delivered, [1, 2])


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


class StartCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_non_owner_receives_group_invitation(self) -> None:
        reply_text = AsyncMock()
        update = SimpleNamespace(
            effective_user=User(id=456, is_bot=False, first_name="user"),
            effective_chat=Chat(id=456, type=Chat.PRIVATE),
            effective_message=SimpleNamespace(reply_text=reply_text),
        )
        context = SimpleNamespace(
            application=SimpleNamespace(
                bot_data={
                    "config": BotConfig("token", 123, "postgresql://localhost/db", 20),
                },
            ),
        )

        await about(update, context)

        reply_text.assert_awaited_once_with("Vem pro grupo ver mais: t.me/pra_bater")


class OwnerCommandMenuTests(unittest.IsolatedAsyncioTestCase):
    async def test_application_configures_timeouts_for_polling_and_bot_api(self) -> None:
        application = create_application(
            BotConfig("123456:TEST", 123, "postgresql://localhost/db", 20),
        )
        polling_request, bot_api_request = application.bot._request
        try:
            self.assertEqual(
                polling_request._client.timeout.read,
                POLLING_READ_TIMEOUT_SECONDS,
            )
            self.assertEqual(
                bot_api_request._client.timeout.read,
                BOT_API_TIMEOUT_SECONDS,
            )
            self.assertEqual(
                bot_api_request._media_write_timeout,
                MEDIA_WRITE_TIMEOUT_SECONDS,
            )
            self.assertIn("delivery_queue", application.bot_data)
            registered_commands = {
                command
                for handler in application.handlers[0]
                if isinstance(handler, CommandHandler)
                for command in handler.commands
            }
            self.assertTrue({"salvar", "apontar"} <= registered_commands)
            self.assertIn("listar_apelidos", registered_commands)
        finally:
            for request in application.bot._request:
                await request.shutdown()

    async def test_registers_commands_only_in_owner_private_chat_scope(self) -> None:
        bot = SimpleNamespace(set_my_commands=AsyncMock())

        await register_owner_commands(bot, owner_id=123)

        bot.set_my_commands.assert_awaited_once()
        commands = bot.set_my_commands.await_args.args[0]
        scope = bot.set_my_commands.await_args.kwargs["scope"]
        self.assertEqual(commands, OWNER_COMMANDS)
        self.assertEqual(scope, BotCommandScopeChat(chat_id=123))
        self.assertIn("salvar", {command.command for command in commands})
        self.assertIn("apontar", {command.command for command in commands})
        self.assertIn("listar_apelidos", {command.command for command in commands})


if __name__ == "__main__":
    unittest.main()

import asyncio
import logging

from telegram import Bot, BotCommand, BotCommandScopeChat, Chat, Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from bot.config import BotConfig, load_config
from bot.db import Database
from bot.handlers import (
    about,
    caption,
    current_topic_id,
    destination,
    help_command,
    handle_message,
    stop_album_buffers,
    status,
    topic,
)

logger = logging.getLogger(__name__)

OWNER_COMMANDS = (
    BotCommand("start", "Iniciar o bot"),
    BotCommand("help", "Mostrar os comandos"),
    BotCommand("destino", "Configurar grupo ou canal de destino"),
    BotCommand("topico", "Configurar ou remover tópico"),
    BotCommand("id_topico", "Consultar o ID do tópico atual"),
    BotCommand("caption", "Configurar a transformação da legenda"),
    BotCommand("status", "Ver a configuração atual"),
)


async def register_owner_commands(bot: Bot, owner_id: int) -> None:
    await bot.set_my_commands(
        OWNER_COMMANDS,
        scope=BotCommandScopeChat(chat_id=owner_id),
    )


async def _post_init(application: Application) -> None:
    config: BotConfig = application.bot_data["config"]
    database = await Database.connect(config.database_url)
    application.bot_data["database"] = database
    logger.info("PostgreSQL connection is ready.")
    await register_owner_commands(application.bot, config.owner_id)
    logger.info("Telegram command menu registered for owner %s.", config.owner_id)


async def _post_shutdown(application: Application) -> None:
    await stop_album_buffers(application)
    database: Database | None = application.bot_data.get("database")
    if database is not None:
        await database.close()


async def _log_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    error = context.error
    if error is None:
        logger.error("Unhandled error while processing update %r", update)
    else:
        logger.error(
            "Unhandled error while processing update %r",
            update,
            exc_info=(type(error), error, error.__traceback__),
        )
    if not isinstance(update, Update):
        return
    message = update.effective_message
    user = update.effective_user
    config: BotConfig = context.application.bot_data["config"]
    chat = update.effective_chat
    if (
        message is not None
        and user is not None
        and user.id == config.owner_id
        and chat is not None
        and chat.type == Chat.PRIVATE
    ):
        await message.reply_text("Ocorreu um erro interno ao processar o pedido. Consulte os logs do bot.")


def create_application(config: BotConfig | None = None) -> Application:
    config = config or load_config()
    application = (
        ApplicationBuilder()
        .token(config.bot_token)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )
    application.bot_data["config"] = config
    application.bot_data["albums"] = {}
    application.bot_data["album_lock"] = asyncio.Lock()
    application.add_handler(CommandHandler("start", about))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("destino", destination))
    application.add_handler(CommandHandler("topico", topic))
    application.add_handler(CommandHandler("id_topico", current_topic_id))
    application.add_handler(CommandHandler("caption", caption))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))
    application.add_error_handler(_log_error)
    return application


def main() -> None:
    config = load_config()
    logging.basicConfig(
        level=config.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logger.info("Starting Telegram media copier.")
    create_application(config).run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
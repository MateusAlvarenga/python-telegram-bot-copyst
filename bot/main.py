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
from telegram.request import HTTPXRequest

from bot.config import BotConfig, load_config
from bot.db import Database
from bot.handlers import (
    _delivery_worker,
    about,
    caption,
    current_topic_id,
    destination,
    help_command,
    handle_message,
    enviar_midias,
    iniciar_agrupamento,
    list_caption_presets,
    list_destination_aliases,
    load_caption_preset,
    load_destination_alias,
    save_caption_preset,
    save_destination_alias,
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
    BotCommand("salvar", "Salvar destino e tópico com apelido"),
    BotCommand("apontar", "Ativar destino e tópico salvos"),
    BotCommand("listar_apelidos", "Listar apelidos, destinos e tópicos"),
    BotCommand("id_topico", "Consultar o ID do tópico atual"),
    BotCommand("caption", "Configurar a transformação da legenda"),
    BotCommand("salvar_caption", "Salvar a configuração atual da caption"),
    BotCommand("listar_captions", "Listar captions salvas"),
    BotCommand("ativar_caption", "Ativar caption pelo ID"),
    BotCommand("iniviar_agrupamento", "Guardar mídias para envio agrupado posterior"),
    BotCommand("iniciar_agrupamento", "Guardar mídias para envio agrupado posterior"),
    BotCommand("enviar_midias", "Enviar mídias guardadas em grupos de até 10"),
    BotCommand("status", "Ver a configuração atual"),
)

BOT_API_TIMEOUT_SECONDS = 30
MEDIA_WRITE_TIMEOUT_SECONDS = 60
POLLING_READ_TIMEOUT_SECONDS = 40


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
    application.bot_data["delivery_worker"] = asyncio.create_task(
        _delivery_worker(application),
        name="telegram-delivery-worker",
    )
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
    bot_api_request = HTTPXRequest(
        connection_pool_size=4,
        connect_timeout=10,
        read_timeout=BOT_API_TIMEOUT_SECONDS,
        write_timeout=BOT_API_TIMEOUT_SECONDS,
        pool_timeout=10,
        media_write_timeout=MEDIA_WRITE_TIMEOUT_SECONDS,
    )
    polling_request = HTTPXRequest(
        connection_pool_size=1,
        connect_timeout=10,
        read_timeout=POLLING_READ_TIMEOUT_SECONDS,
        write_timeout=10,
        pool_timeout=10,
    )
    application = (
        ApplicationBuilder()
        .token(config.bot_token)
        .request(bot_api_request)
        .get_updates_request(polling_request)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )
    application.bot_data["config"] = config
    application.bot_data["albums"] = {}
    application.bot_data["album_lock"] = asyncio.Lock()
    application.bot_data["delivery_queue"] = asyncio.Queue()
    application.bot_data["grouping_active"] = False
    application.bot_data["grouped_media"] = []
    application.add_handler(CommandHandler("start", about))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("destino", destination))
    application.add_handler(CommandHandler("topico", topic))
    application.add_handler(CommandHandler("salvar", save_destination_alias))
    application.add_handler(CommandHandler("apontar", load_destination_alias))
    application.add_handler(CommandHandler("listar_apelidos", list_destination_aliases))
    application.add_handler(CommandHandler("id_topico", current_topic_id))
    application.add_handler(CommandHandler("caption", caption))
    application.add_handler(CommandHandler("salvar_caption", save_caption_preset))
    application.add_handler(CommandHandler("listar_captions", list_caption_presets))
    application.add_handler(CommandHandler("ativar_caption", load_caption_preset))
    application.add_handler(CommandHandler("iniviar_agrupamento", iniciar_agrupamento))
    application.add_handler(CommandHandler("iniciar_agrupamento", iniciar_agrupamento))
    application.add_handler(CommandHandler("enviar_midias", enviar_midias))
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
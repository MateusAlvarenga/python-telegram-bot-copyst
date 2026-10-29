import asyncio
import html
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Sequence, TypeVar

from telegram import (
    Chat,
    InputMediaAudio,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
    Message,
    MessageEntity,
    Update,
)
from telegram.error import RetryAfter, TelegramError, TimedOut
from telegram.ext import Application, ContextTypes

from bot.config import BotConfig
from bot.db import BotSettings, Database

logger = logging.getLogger(__name__)
ALBUM_WAIT_SECONDS = 0.7
MAX_TIMEOUT_ATTEMPTS = 5
DESTINATION_USERNAME = re.compile(r"^@[A-Za-z][A-Za-z0-9_]{4,31}$")
DESTINATION_ALIAS = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
T = TypeVar("T")


@dataclass
class DeliveryJob:
    messages: list[Message]
    context: ContextTypes.DEFAULT_TYPE
    ready: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass
class AlbumBuffer:
    job: DeliveryJob
    task: asyncio.Task[None] | None = None


def _settings(context: ContextTypes.DEFAULT_TYPE) -> Database:
    return context.application.bot_data["database"]


def _config(context: ContextTypes.DEFAULT_TYPE) -> BotConfig:
    return context.application.bot_data["config"]


async def _is_owner(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    chat = update.effective_chat
    if user is None or user.id != _config(context).owner_id or chat is None:
        return False
    return chat.type == Chat.PRIVATE


async def _reply(update: Update, text: str) -> None:
    message = update.effective_message
    if message is not None:
        await message.reply_text(text)


async def about(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _is_owner(update, context):
        await _reply(
            update,
            "Bot copista pronto. Envie uma mídia ou encaminhe uma mensagem com mídia "
            "para copiá-la ao destino configurado. Use /help para ver os comandos.",
        )
    else:
        await _reply(update, "Vem pro grupo ver mais: t.me/pra_bater")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_owner(update, context):
        return
    await _reply(
        update,
        "Comandos (somente no chat privado do proprietário):\n"
        "/destino <chat_id|@username> — escolher grupo ou canal de destino\n"
        "/topico <message_thread_id|off> — definir ou remover o tópico\n"
        "/salvar <apelido> — salvar o destino e tópico atuais\n"
        "/apontar <apelido> — ativar destino e tópico salvos\n"
        "/listar_apelidos — listar destinos e tópicos salvos\n"
        "/caption original — manter a legenda original\n"
        "/caption append <texto> — acrescentar texto em nova linha\n"
        "/caption prepend <texto> — inserir texto em nova linha antes da legenda\n"
        "/caption replace_parcial <buscar> => <substituir> — substituir todas as ocorrências "
        "literais (diferencia maiúsculas de minúsculas)\n"
        "/caption replace_total <texto> — substituir toda a legenda\n"
        "/salvar_caption — salvar a caption atual com um ID sequencial\n"
        "/listar_captions — listar captions salvas\n"
        "/ativar_caption <id> — ativar uma caption salva\n"
        "/status — mostrar configuração atual",
    )


async def destination(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_owner(update, context):
        return
    if len(context.args) != 1:
        await _reply(update, "Uso: /destino <chat_id|@username>")
        return

    destination_value = context.args[0]
    numeric_chat_id = (
        int(destination_value)
        if re.fullmatch(r"-?[0-9]{1,20}", destination_value)
        else None
    )
    is_chat_id = (
        numeric_chat_id is not None
        and numeric_chat_id != 0
        and -(2**63) <= numeric_chat_id < 2**63
    )
    if not is_chat_id and not DESTINATION_USERNAME.fullmatch(destination_value):
        await _reply(update, "Informe um chat_id numérico ou @username válido.")
        return

    try:
        chat = await context.bot.get_chat(
            numeric_chat_id if is_chat_id else destination_value
        )
    except TelegramError as exc:
        logger.warning("Could not validate destination %s: %s", destination_value, exc)
        await _reply(
            update,
            "Não consegui acessar esse destino. Verifique o ID/@username e "
            "se o bot tem acesso ao grupo ou canal.",
        )
        return

    if chat.type not in {Chat.GROUP, Chat.SUPERGROUP, Chat.CHANNEL}:
        await _reply(update, "O destino precisa ser um grupo, supergrupo ou canal.")
        return

    await _settings(context).set_destination(str(chat.id))
    await _reply(
        update,
        f"Destino configurado: {chat.title or chat.username or chat.id}. "
        "A configuração anterior de tópico foi removida.",
    )


async def topic(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_owner(update, context):
        return
    if len(context.args) != 1:
        await _reply(update, "Uso: /topico <message_thread_id|off>")
        return

    settings = await _settings(context).get_settings()
    if settings.destination_chat_id is None:
        await _reply(update, "Configure primeiro o destino com /destino.")
        return
    if context.args[0].lower() == "off":
        await _settings(context).set_topic(None)
        await _reply(update, "Envio para tópico específico desativado.")
        return
    if not re.fullmatch(r"[0-9]{1,19}", context.args[0]):
        await _reply(update, "O ID do tópico deve ser um número positivo ou off.")
        return

    try:
        chat = await context.bot.get_chat(settings.destination_chat_id)
    except TelegramError as exc:
        logger.warning("Could not validate the configured destination: %s", exc)
        await _reply(update, "Não consegui verificar o destino. Configure-o novamente com /destino.")
        return
    if chat.type != Chat.SUPERGROUP or not chat.is_forum:
        await _reply(update, "O destino precisa ser um supergrupo com tópicos habilitados.")
        return

    topic_id = int(context.args[0])
    if topic_id >= 2**63:
        await _reply(update, "O ID do tópico está fora do intervalo permitido.")
        return
    await _settings(context).set_topic(topic_id)
    await _reply(update, f"Tópico configurado: {topic_id}.")


async def save_destination_alias(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not await _is_owner(update, context):
        return
    if len(context.args) != 1 or not DESTINATION_ALIAS.fullmatch(context.args[0]):
        await _reply(
            update,
            "Uso: /salvar <apelido> (letras, números, _ ou -, até 32 caracteres)",
        )
        return

    settings = await _settings(context).get_settings()
    if settings.destination_chat_id is None:
        await _reply(update, "Configure primeiro o destino com /destino.")
        return

    alias = context.args[0]
    await _settings(context).save_destination_alias(
        alias,
        settings.destination_chat_id,
        settings.topic_id,
    )
    await _reply(
        update,
        f"Destino e tópico atuais salvos como '{alias}'. "
        "Se o apelido já existia, a configuração foi substituída.",
    )


async def load_destination_alias(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not await _is_owner(update, context):
        return
    if len(context.args) != 1 or not DESTINATION_ALIAS.fullmatch(context.args[0]):
        await _reply(
            update,
            "Uso: /apontar <apelido> (letras, números, _ ou -, até 32 caracteres)",
        )
        return

    alias = context.args[0]
    saved_settings = await _settings(context).get_destination_alias(alias)
    if saved_settings is None:
        await _reply(update, f"Não existe configuração salva com o apelido '{alias}'.")
        return

    destination_chat_id, topic_id = saved_settings
    await _settings(context).set_destination_and_topic(destination_chat_id, topic_id)
    topic_value = str(topic_id) if topic_id is not None else "nenhum"
    await _reply(
        update,
        f"Configuração '{alias}' ativada: destino {destination_chat_id}; "
        f"tópico {topic_value}.",
    )


async def list_destination_aliases(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not await _is_owner(update, context):
        return

    aliases = await _settings(context).list_destination_aliases()
    if not aliases:
        await _reply(update, "Nenhum apelido salvo.")
        return

    rows = [
        (alias, destination, str(topic) if topic is not None else "nenhum")
        for alias, destination, topic in aliases
    ]
    headers = ("Apelido", "Destino", "Tópico")
    widths = tuple(
        max(len(headers[index]), *(len(row[index]) for row in rows))
        for index in range(len(headers))
    )

    def format_row(row: tuple[str, str, str]) -> str:
        return " | ".join(value.ljust(width) for value, width in zip(row, widths))

    header = format_row(headers)
    separator = "-+-".join("-" * width for width in widths)
    table_rows = [header, separator, *(format_row(row) for row in rows)]
    chunks: list[list[str]] = []
    current_rows = [table_rows[0], table_rows[1]]
    for row in table_rows[2:]:
        if sum(map(len, current_rows)) + len(current_rows) + len(row) + 1 > 3800:
            chunks.append(current_rows)
            current_rows = [table_rows[0], table_rows[1]]
        current_rows.append(row)
    chunks.append(current_rows)

    message = update.effective_message
    if message is None:
        return
    for chunk in chunks:
        table = "\n".join(chunk)
        await message.reply_text(f"<pre>{html.escape(table)}</pre>", parse_mode="HTML")


async def current_topic_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    message = update.effective_message
    if (
        user is None
        or user.id != _config(context).owner_id
        or chat is None
        or chat.type != Chat.SUPERGROUP
        or message is None
    ):
        return
    if message.message_thread_id is None:
        await context.bot.send_message(
            chat_id=user.id,
            text="Envie /id_topico dentro do tópico desejado, não na conversa geral.",
        )
        return
    await context.bot.send_message(
        chat_id=user.id,
        text=f"ID do tópico atual: {message.message_thread_id}\n"
        f"Configure-o no privado com /topico {message.message_thread_id}.",
    )


async def caption(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_owner(update, context):
        return
    message = update.effective_message
    raw_text = message.text if message is not None else None
    if not raw_text:
        await _reply(
            update,
            "Uso: /caption original|append|prepend|replace_parcial|replace_total ...",
        )
        return

    command_parts = raw_text.split(maxsplit=1)
    if len(command_parts) < 2:
        await _reply(
            update,
            "Uso: /caption original|append|prepend|replace_parcial|replace_total ...",
        )
        return
    mode, separator, payload = command_parts[1].partition(" ")
    mode = mode.lower()
    payload = payload.lstrip() if separator else ""

    if mode == "original" and not payload:
        await _settings(context).set_caption("original")
        await _reply(update, "A legenda original será mantida.")
        return
    if mode in {"append", "prepend"} and payload.strip():
        await _settings(context).set_caption(mode, text=payload.strip())
        action = "acrescentado ao final" if mode == "append" else "inserido no início"
        await _reply(update, f"Texto da legenda configurado para ser {action}.")
        return
    if mode == "replace_total" and payload.strip():
        await _settings(context).set_caption("replace_total", text=payload.strip())
        await _reply(update, "A legenda será totalmente substituída.")
        return
    if mode == "replace_parcial" and "=>" in payload:
        search, replacement = payload.split("=>", maxsplit=1)
        search = search.strip()
        replacement = replacement.strip()
        if search:
            await _settings(context).set_caption(
                "replace_parcial",
                search=search,
                replacement=replacement,
            )
            await _reply(update, "Substituição de legenda configurada.")
            return

    await _reply(
        update,
        "Uso: /caption original | /caption append <texto> | /caption prepend <texto> | "
        "/caption replace_parcial <buscar> => <substituir> | "
        "/caption replace_total <texto>",
    )


def _caption_preset_description(
    mode: str,
    text: str | None,
    search: str | None,
    replacement: str | None,
) -> str:
    if mode == "original":
        return "manter original"
    if mode in {"append", "prepend"}:
        return f"{mode}: {(text or '').replace(chr(10), ' ↵ ')}"
    if mode == "replace_parcial":
        return f"substituir {search!r} por {replacement!r}"
    if mode == "replace_total":
        return f"substituir por: {(text or '').replace(chr(10), ' ↵ ')}"
    return mode


async def save_caption_preset(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not await _is_owner(update, context):
        return
    if context.args:
        await _reply(update, "Uso: /salvar_caption")
        return

    settings = await _settings(context).get_settings()
    preset_id = await _settings(context).save_caption_preset(
        settings.caption_mode,
        text=settings.caption_text,
        search=settings.caption_search,
        replacement=settings.caption_replacement,
    )
    await _reply(update, f"Caption atual salva com o ID {preset_id}.")


async def load_caption_preset(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not await _is_owner(update, context):
        return
    if len(context.args) != 1 or not context.args[0].isdigit():
        await _reply(update, "Uso: /ativar_caption <id>")
        return

    preset_id = int(context.args[0])
    preset = await _settings(context).get_caption_preset(preset_id)
    if preset is None:
        await _reply(update, f"Não existe caption salva com o ID {preset_id}.")
        return

    mode, text, search, replacement = preset
    await _settings(context).set_caption(
        mode,
        text=text,
        search=search,
        replacement=replacement,
    )
    await _reply(update, f"Caption ID {preset_id} ativada.")


async def list_caption_presets(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not await _is_owner(update, context):
        return

    presets = await _settings(context).list_caption_presets()
    if not presets:
        await _reply(update, "Nenhuma caption salva.")
        return

    rows = []
    for preset_id, mode, text, search, replacement in presets:
        description = _caption_preset_description(mode, text, search, replacement)
        rows.append((str(preset_id), description.replace("|", "\\|")))
    headers = ("ID", "Caption")
    widths = tuple(
        max(len(headers[index]), *(len(row[index]) for row in rows))
        for index in range(len(headers))
    )

    def format_row(row: tuple[str, str]) -> str:
        return " | ".join(value.ljust(width) for value, width in zip(row, widths))

    header = format_row(headers)
    separator = "-+-".join("-" * width for width in widths)
    table_rows = [header, separator, *(format_row(row) for row in rows)]
    chunks = []
    current_rows = table_rows[:2]
    for row in table_rows[2:]:
        if sum(map(len, current_rows)) + len(current_rows) + len(row) + 1 > 3800:
            chunks.append(current_rows)
            current_rows = table_rows[:2]
        current_rows.append(row)
    chunks.append(current_rows)

    message = update.effective_message
    if message is None:
        return
    for chunk in chunks:
        await message.reply_text(
            f"<pre>{html.escape(chr(10).join(chunk))}</pre>",
            parse_mode="HTML",
        )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_owner(update, context):
        return
    settings = await _settings(context).get_settings()
    destination_value = settings.destination_chat_id or "não configurado"
    topic_value = str(settings.topic_id) if settings.topic_id is not None else "nenhum"
    if settings.caption_mode == "original":
        caption_value = "manter legenda original"
    elif settings.caption_mode in {"append", "prepend"}:
        caption_value = f"{settings.caption_mode}: {settings.caption_text}"
    elif settings.caption_mode == "replace_parcial":
        caption_value = (
            f"replace_parcial: {settings.caption_search!r} "
            f"=> {settings.caption_replacement!r}"
        )
    else:
        caption_value = f"replace_total: {settings.caption_text}"
    await _reply(
        update,
        f"Destino: {destination_value}\nTópico: {topic_value}\nLegenda: {caption_value}",
    )


def _utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def transform_caption(
    caption_text: str | None,
    entities: Sequence[MessageEntity] | None,
    settings: BotSettings,
) -> tuple[str | None, list[MessageEntity] | None, bool]:
    if settings.caption_mode == "original":
        return caption_text, list(entities) if entities else None, False
    if settings.caption_mode in {"append", "prepend"}:
        addition = settings.caption_text or ""
        original = caption_text or ""
        separator = "\n" if original and addition else ""
        if settings.caption_mode == "append":
            transformed = f"{original}{separator}{addition}"
            adjusted_entities = list(entities) if entities else None
        else:
            prefix = f"{addition}{separator}"
            transformed = f"{prefix}{original}"
            adjusted_entities = list(entities) if entities else None
            if adjusted_entities is not None:
                shift = _utf16_length(prefix)
                adjusted_entities = [
                    MessageEntity.de_json(
                        {**entity.to_dict(), "offset": entity.offset + shift},
                        bot=None,
                    )
                    for entity in adjusted_entities
                ]
        return transformed, adjusted_entities, True
    if settings.caption_mode == "replace_parcial":
        search = settings.caption_search or ""
        if not caption_text or not search:
            return caption_text, list(entities) if entities else None, False
        transformed = caption_text.replace(search, settings.caption_replacement or "")
        if transformed == caption_text:
            return caption_text, list(entities) if entities else None, False
        return transformed, None, True
    if settings.caption_mode == "replace_total":
        return settings.caption_text or "", None, True
    raise ValueError(f"Unsupported caption mode: {settings.caption_mode}")


def _can_have_caption(message: Message) -> bool:
    return message.caption is not None or any(
        getattr(message, field_name, None) is not None
        for field_name in ("animation", "audio", "document", "photo", "video", "voice")
    )


def _has_media(message: Message) -> bool:
    return message.effective_attachment is not None


def _media_input(
    message: Message,
    settings: BotSettings,
    transform: bool,
) -> InputMediaAudio | InputMediaDocument | InputMediaPhoto | InputMediaVideo:
    caption_text = message.caption
    caption_entities = message.caption_entities
    should_override = False
    if transform:
        caption_text, caption_entities, should_override = transform_caption(
            caption_text,
            caption_entities,
            settings,
        )
    caption_options: dict[str, object] = {}
    if caption_text is not None or should_override:
        caption_options = {
            "caption": caption_text,
            "caption_entities": caption_entities,
            "parse_mode": None,
        }

    if message.photo:
        return InputMediaPhoto(
            message.photo[-1].file_id,
            has_spoiler=message.has_media_spoiler,
            show_caption_above_media=message.show_caption_above_media,
            **caption_options,
        )
    if message.video is not None:
        return InputMediaVideo(
            message.video.file_id,
            width=message.video.width,
            height=message.video.height,
            duration=message.video.duration,
            has_spoiler=message.has_media_spoiler,
            show_caption_above_media=message.show_caption_above_media,
            **caption_options,
        )
    if message.audio is not None:
        return InputMediaAudio(
            message.audio.file_id,
            duration=message.audio.duration,
            performer=message.audio.performer,
            title=message.audio.title,
            **caption_options,
        )
    if message.document is not None:
        return InputMediaDocument(
            message.document.file_id,
            filename=message.document.file_name,
            **caption_options,
        )
    raise ValueError("Este tipo de mídia não pode ser incluído em um álbum.")


async def _copy_single(
    message: Message,
    settings: BotSettings,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    kwargs: dict[str, object] = {}
    if _can_have_caption(message):
        transformed, entities, should_override = transform_caption(
            message.caption,
            message.caption_entities,
            settings,
        )
        if should_override:
            kwargs["caption"] = transformed
            kwargs["caption_entities"] = entities
            kwargs["parse_mode"] = None
    await context.bot.copy_message(
        chat_id=settings.destination_chat_id,
        from_chat_id=message.chat_id,
        message_id=message.message_id,
        message_thread_id=settings.topic_id,
        **kwargs,
    )


async def _copy_album(
    messages: list[Message],
    settings: BotSettings,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    messages.sort(key=lambda item: item.message_id)
    if len(messages) == 1:
        await _copy_single(messages[0], settings, context)
        return
    if len(messages) > 10:
        raise ValueError("O álbum excede o limite de 10 mídias da Bot API.")

    caption_index = next(
        (index for index, message in enumerate(messages) if message.caption is not None),
        None,
    )
    if caption_index is None and settings.caption_mode in {
        "append",
        "prepend",
        "replace_total",
    }:
        caption_index = 0
    media = [
        _media_input(message, settings, transform=(index == caption_index))
        for index, message in enumerate(messages)
    ]
    await context.bot.send_media_group(
        chat_id=settings.destination_chat_id,
        media=media,
        message_thread_id=settings.topic_id,
    )


async def _call_with_retries(operation: Callable[[], Awaitable[T]]) -> T:
    timeout_attempts = 0
    while True:
        try:
            return await operation()
        except RetryAfter as exc:
            delay = (
                exc.retry_after.total_seconds()
                if isinstance(exc.retry_after, timedelta)
                else float(exc.retry_after)
            )
            logger.info("Telegram rate limit reached; retrying in %.2f seconds.", delay)
            await asyncio.sleep(delay)
        except TimedOut:
            timeout_attempts += 1
            if timeout_attempts >= MAX_TIMEOUT_ATTEMPTS:
                raise
            delay = min(2 ** (timeout_attempts - 1), 30)
            logger.warning(
                "Telegram request timed out; retrying in %s seconds (attempt %s/%s).",
                delay,
                timeout_attempts + 1,
                MAX_TIMEOUT_ATTEMPTS,
            )
            await asyncio.sleep(delay)


async def _notify_delivery_failure(message: Message, error: Exception) -> None:
    try:
        await _call_with_retries(
            lambda: message.reply_text(f"Não foi possível copiar a mídia: {error}")
        )
    except TelegramError:
        logger.exception("Could not notify the owner about a failed delivery.")


async def _deliver_messages(job: DeliveryJob) -> None:
    messages = job.messages
    context = job.context
    if not messages:
        return
    settings = await _settings(context).get_settings()
    source = messages[0]
    if settings.destination_chat_id is None:
        await _call_with_retries(
            lambda: source.reply_text(
                "Nenhum destino configurado. Defina-o com /destino <chat_id|@username>."
            )
        )
        return

    try:
        if len(messages) > 1:
            await _call_with_retries(
                lambda: _copy_album(messages, settings, context)
            )
        else:
            await _call_with_retries(
                lambda: _copy_single(source, settings, context)
            )
    except (TelegramError, ValueError) as exc:
        logger.warning("Could not copy message(s) to %s: %s", settings.destination_chat_id, exc)
        await _notify_delivery_failure(source, exc)
        return
    try:
        await _call_with_retries(lambda: source.reply_text("Mídia copiada."))
    except TelegramError:
        logger.exception("Media was copied but its confirmation could not be sent.")


async def _delivery_worker(application: Application) -> None:
    queue: asyncio.Queue[DeliveryJob] = application.bot_data["delivery_queue"]
    while True:
        job = await queue.get()
        try:
            await job.ready.wait()
            await _deliver_messages(job)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            message = job.messages[0] if job.messages else None
            logger.exception("Unexpected error processing queued Telegram delivery.")
            if message is not None:
                await _notify_delivery_failure(message, exc)
        finally:
            queue.task_done()


def _enqueue_delivery(
    messages: list[Message],
    context: ContextTypes.DEFAULT_TYPE,
    *,
    wait_until_ready: bool = False,
) -> DeliveryJob:
    job = DeliveryJob(messages=messages, context=context)
    if not wait_until_ready:
        job.ready.set()
    queue: asyncio.Queue[DeliveryJob] = context.application.bot_data["delivery_queue"]
    queue.put_nowait(job)
    return job


async def _flush_album_after_delay(
    key: tuple[int, str],
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    try:
        await asyncio.sleep(ALBUM_WAIT_SECONDS)
    except asyncio.CancelledError:
        return

    lock: asyncio.Lock = context.application.bot_data["album_lock"]
    async with lock:
        pending: dict[tuple[int, str], AlbumBuffer] = context.application.bot_data["albums"]
        album = pending.pop(key, None)
    if album is not None:
        album.job.ready.set()


async def _queue_album(
    message: Message,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    key = (message.chat_id, message.media_group_id or "")
    lock: asyncio.Lock = context.application.bot_data["album_lock"]
    async with lock:
        pending: dict[tuple[int, str], AlbumBuffer] = context.application.bot_data["albums"]
        album = pending.get(key)
        if album is None:
            job = _enqueue_delivery([], context, wait_until_ready=True)
            album = AlbumBuffer(job=job)
            pending[key] = album
        elif album.task is not None:
            album.task.cancel()
        album.job.messages.append(message)
        album.task = asyncio.create_task(_flush_album_after_delay(key, context))


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_owner(update, context):
        return
    message = update.effective_message
    if message is None:
        return
    if message.media_group_id:
        await _queue_album(message, context)
        return
    if not _has_media(message):
        await _reply(update, "Envie uma mídia compatível ou encaminhe uma mensagem com mídia.")
        return
    _enqueue_delivery([message], context)


async def stop_album_buffers(application: Application) -> None:
    pending: dict[tuple[int, str], AlbumBuffer] = application.bot_data.get("albums", {})
    tasks = [album.task for album in pending.values() if album.task is not None]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    worker: asyncio.Task[None] | None = application.bot_data.get("delivery_worker")
    if worker is not None:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
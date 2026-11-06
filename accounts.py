"""Explicit imports only. Never scans a machine for existing account sessions."""
import asyncio
from contextlib import closing
import io
import json
from pathlib import Path
import re
import sqlite3
import tempfile
import zipfile

from telethon import TelegramClient, functions
from telethon.crypto import AuthKey
from telethon.sessions import StringSession


class InputError(ValueError):
    pass


def api_options(meta, default_id, default_hash):
    result = {"api_id": int(meta.get("app_id") or meta.get("api_id") or default_id),
              "api_hash": meta.get("app_hash") or meta.get("api_hash") or default_hash}
    for source, target in (("device", "device_model"), ("sdk", "system_version"),
                           ("app_version", "app_version"), ("lang_code", "lang_code"),
                           ("system_lang_code", "system_lang_code")):
        if meta.get(source):
            result[target] = str(meta[source])
    if result["api_id"] <= 0 or not re.fullmatch(r"[a-fA-F0-9]{32}", result["api_hash"]):
        raise InputError("Нужны корректные TELEGRAM_API_ID и TELEGRAM_API_HASH")
    return result


def make_client(session, options):
    return TelegramClient(session, **options, flood_sleep_threshold=0, request_retries=0,
                          raise_last_call_error=True, connection_retries=1,
                          timeout=15, receive_updates=False)


async def dc_address(dc_id, options):
    probe = make_client(StringSession(), options)
    try:
        await probe.connect()
        config = await probe(functions.help.GetConfigRequest())
        candidates = [dc for dc in config.dc_options if dc.id == dc_id
                      and not dc.ipv6 and not dc.media_only and not dc.cdn
                      and not dc.tcpo_only and not dc.secret]
        if not candidates:
            raise InputError("Telegram не вернул адрес указанного DC")
        return candidates[0].ip_address, candidates[0].port
    finally:
        await probe.disconnect()


async def from_key(raw_key, dc_id, options, address=None, port=443):
    if isinstance(raw_key, str):
        raw_key = re.sub(r"\s", "", raw_key)
        if not re.fullmatch(r"[a-fA-F0-9]{512}", raw_key):
            raise InputError("Auth Key должен быть полным: 512 HEX-символов, без многоточия")
        raw_key = bytes.fromhex(raw_key)
    if not isinstance(raw_key, bytes) or len(raw_key) != 256 or not any(raw_key):
        raise InputError("Auth Key должен содержать 256 байт")
    dc_id = int(dc_id)
    if dc_id not in {1, 2, 3, 4, 5}:
        raise InputError("Поддерживаются production DC ID от 1 до 5")
    # Resolve DC through Telegram, rather than trusting an uploaded server address.
    address, port = await dc_address(dc_id, options)
    session = StringSession()
    session.set_dc(dc_id, address, port)
    session.auth_key = AuthKey(raw_key)
    return make_client(session, options)


async def from_sqlite(data, kind, options):
    if not data.startswith(b"SQLite format 3\x00"):
        raise InputError("Файл не является SQLite-сессией")
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "upload.session"
        path.write_bytes(data)
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
            db.execute("PRAGMA query_only=ON")
            db.execute("PRAGMA trusted_schema=OFF")
            if kind == "telethon":
                row = db.execute("SELECT dc_id,auth_key FROM sessions LIMIT 1").fetchone()
            else:
                row = db.execute("SELECT dc_id,auth_key,test_mode,is_bot FROM sessions LIMIT 1").fetchone()
                if row and (row[2] or row[3]):
                    raise InputError("Нужна production-сессия личного аккаунта")
        if not row:
            raise InputError("В сессии нет данных авторизации")
        return await from_key(row[1], row[0], options)


def unpack_tdata(data, folder):
    root = Path(folder).resolve()
    total = 0
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entries = archive.infolist()
        if len(entries) > 2000:
            raise InputError("Слишком много файлов в ZIP")
        for item in entries:
            name = item.filename.replace("\\", "/")
            destination = (root / name).resolve()
            if not destination.is_relative_to(root) or ":" in name:
                raise InputError("Небезопасный путь в ZIP")
            if (item.external_attr >> 16) & 0o170000 == 0o120000:
                raise InputError("Символические ссылки в ZIP запрещены")
            total += item.file_size
            if total > 40 * 1024 * 1024:
                raise InputError("Распакованный TData больше 40 МБ")
            if item.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(archive.read(item))
    candidates = list(root.rglob("key_datas"))
    if len(candidates) != 1:
        raise InputError("В ZIP должна быть одна папка TData с файлом key_datas")
    return candidates[0].parent


async def from_tdata(data, passcode):
    from opentele2.td import TDesktop
    from opentele2.api import UseCurrentSession
    with tempfile.TemporaryDirectory() as folder:
        path = unpack_tdata(data, folder)
        desktop = await asyncio.to_thread(TDesktop, str(path), passcode=passcode or None)
        if desktop.accountsCount != 1:
            raise InputError("Загрузите TData с одним аккаунтом")
        return await desktop.ToTelethon(StringSession(), UseCurrentSession,
                                       flood_sleep_threshold=0, request_retries=0,
                                       raise_last_call_error=True)


async def validate(client, options=None, expected_user=None):
    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise InputError("Сессия не авторизована или отозвана. Используйте вход по номеру")
        user = await client.get_me()
        if not user or user.bot:
            raise InputError("Нужен личный аккаунт Telegram")
        if expected_user and user.id != int(expected_user):
            raise InputError("User ID не совпадает с аккаунтом сессии")
        payload = {"session": StringSession.save(client.session),
                   "options": options or {"api_id": client.api_id, "api_hash": client.api_hash}}
        label = (user.first_name or "Аккаунт")[:50] + f" · {user.id}"
        return user.id, label, payload
    finally:
        await client.disconnect()


def parse_json(data):
    value = json.loads(data.decode("utf-8-sig") if isinstance(data, bytes) else data)
    if not isinstance(value, dict):
        raise InputError("Ожидается JSON-объект")
    return value


def error_text(error):
    # Never print exception messages or request repr: they may contain credentials.
    name = type(error).__name__
    if isinstance(error, InputError):
        return str(error)
    explanations = {
        "PhoneNumberInvalidError": "Telegram не принимает этот номер. Проверьте международный формат.",
        "PhoneNumberBannedError": "Telegram заблокировал номер.",
        "PhoneCodeInvalidError": "Неверный код входа. Попробуйте снова.",
        "PhoneCodeExpiredError": "Код истёк. Начните вход заново.",
        "PasswordHashInvalidError": "Неверный пароль 2FA. Попробуйте снова.",
        "ApiIdInvalidError": "Telegram не принимает API ID/API Hash.",
        "AuthKeyUnregisteredError": "Ключ сессии отозван или не зарегистрирован.",
        "AuthKeyDuplicatedError": "Одна сессия использована с разных IP. Нужен новый вход.",
        "SendCodeUnavailableError": "Telegram сейчас не может выдать код. Повторите позже или импортируйте сессию.",
        "InviteRequestSentError": "Заявка на вступление отправлена; нужно одобрение администратора.",
        "ChatWriteForbiddenError": "У аккаунта нет права писать в этот чат.",
        "UserBannedInChannelError": "Аккаунт заблокирован в этом чате.",
        "ChannelPrivateError": "Нет доступа к группе или каналу.",
        "ChatAdminRequiredError": "Для этого действия нужны права администратора.",
        "ChatSendMediaForbiddenError": "В этом чате запрещена отправка медиа.",
        "ChatSendPhotosForbiddenError": "В этом чате запрещены фотографии.",
        "ChatSendVideosForbiddenError": "В этом чате запрещены видео.",
        "ChatSendPlainForbiddenError": "В этом чате запрещены текстовые сообщения.",
        "UserNotParticipantError": "Аккаунт не состоит в этом чате.",
        "ChannelsTooMuchError": "Достигнут лимит групп и каналов аккаунта.",
        "InviteHashExpiredError": "Ссылка приглашения истекла.",
        "InviteHashInvalidError": "Недействительная ссылка приглашения или папки.",
        "PeerFloodError": "Telegram ограничил действия аккаунта. Задача остановлена.",
        "TimeoutError": "Сеть не ответила вовремя. Проверьте статус задачи перед повтором.",
        "ConnectionError": "Нет соединения с Telegram.",
    }
    if hasattr(error, "seconds"):
        return f"{name}: Telegram требует ждать {int(error.seconds)} сек."
    return f"{name}: {explanations.get(name, 'Операция не выполнена. Проверьте формат данных и доступ аккаунта.')}"

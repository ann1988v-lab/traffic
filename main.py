"""Telegram control panel with separate private workspaces for every user."""
import asyncio
from contextlib import closing
import io
import json
import logging
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time

from dotenv import load_dotenv
from telegram import InlineKeyboardButton as Button, InlineKeyboardMarkup as Keyboard
from telegram.ext import (Application, BaseUpdateProcessor, CallbackQueryHandler,
                          CommandHandler, MessageHandler, filters)
from telethon import errors
from telethon.sessions import StringSession

from accounts import (InputError, api_options, error_text, from_key, from_sqlite,
                      from_tdata, make_client, parse_json, validate)
from delivery import Runner, parse_targets, prepare, report_csv
from storage import Store

ROOT = Path(__file__).resolve().parent
MAX_UPLOAD = 18 * 1024 * 1024


def keyboard(rows):
    result = []
    for row in rows:
        buttons = []
        for label, value in row:
            style = ("success" if value.startswith(("run:", "resume:")) or value == "preview" else
                     "danger" if value == "stop" or value.startswith(("delete:", "remove:")) else
                     None if value in {"menu", "settings", "help"} else "primary")
            buttons.append(Button(label, callback_data=value, style=style))
        result.append(buttons)
    return Keyboard(result)


MENU = keyboard([
    [("1 · Аккаунты", "accounts"), ("➕ Подключить", "add")],
    [("2 · Сообщение", "message"), ("3 · Чаты / addlist", "targets")],
    [("4 · Настройки", "settings")],
    [("5 · Проверить и запустить", "preview")],
    [("📊 Статус и отчёты", "status"), ("⏹ Стоп", "stop")],
    [("❔ Справка", "help")],
])

SETTING_INFO = {
    "cycles": ("Количество циклов", 1, 1000, [1, 2, 3, 5, 10]),
    "delay": ("Пауза между сообщениями (секунды)", 1, 86400, [1, 10, 30, 60]),
    "repeat": ("Пауза между циклами (секунды)", 0, 2592000, [0, 60, 300, 3600]),
}

HELP_TEXT = {
    "account": "АККАУНТЫ\nДобавить аккаунт → JSON → отправьте JSON файлом → следующим сообщением .session "
               "того же аккаунта. Между файлами не начинайте подключение заново.\n"
               "В разделе «Аккаунты» ✅ означает участие в рассылке. Каждый выбранный аккаунт пишет во все чаты.",
    "message": "СООБЩЕНИЕ\nНажмите «Сообщение» и отправьте текст либо одно фото/видео с подписью. "
               "Файл до 18 МБ, подпись до 1024 символов. Обычный текст заменяет вложение. Альбомы не поддерживаются.",
    "targets": "ЧАТЫ И ПАПКИ\nНажмите «Чаты / addlist» и отправьте ссылки по одной на строку. "
               "Можно несколько addlist сразу: группы всех папок объединятся, повторы уберутся.\n"
               "Поддерживаются @username, t.me/username, t.me/+приглашение и t.me/addlist/папка.\n"
               "Если аккаунт уже состоит в группе, повторное вступление не нужно. "
               "В недостающие группы бот попробует вступить при запуске. Заявки требуют одобрения администратора.",
    "settings": "НАСТРОЙКИ\nЦиклы: сколько раз пройти весь список чатов. Например, 3 — три полных прохода.\n"
                "Между сообщениями: пауза перед следующей отправкой.\n"
                "Между циклами: пауза после окончания списка перед новым проходом. "
                "При одном цикле она не используется.\nВыберите параметр кнопкой, затем готовое значение или «Своё число».",
    "report": "ЗАПУСК И ОТЧЁТ\n«Проверить и запустить» собирает план без отправок и вступлений. "
              "Проверьте список и нажмите «Запустить». После изменения настроек создайте новый план.\n"
              "Ошибка отдельного чата не останавливает остальные. В конце придут итог и CSV с названиями, "
              "ссылками и причинами ошибок.\nsent — отправлено; failed — не отправлено; "
              "pending — нет подтверждения, проверьте вручную.\n"
              "FloodWait: бот ждёт указанное Telegram время на том же аккаунте. "
              "Общее ограничение аккаунта PeerFlood может прервать задачу. Остановка: «Стоп» или /stop.",
}

IMPORT_MENU = keyboard([
    [("Номер телефона", "import:phone"), ("JSON", "import:json")],
    [(".session Telethon", "import:telethon"), (".session Pyrogram", "import:pyrogram")],
    [("Auth Key + DC ID", "import:key"), ("StringSession Telethon", "import:string")],
    [("TData ZIP", "import:tdata")],
    [("В меню", "menu")],
])


def allowed(update, owner):
    return (update.effective_user is not None and update.effective_user.id == owner
            and update.effective_chat is not None and update.effective_chat.type == "private")


class Panel:
    def __init__(self, store, owner, default_api_id, default_api_hash):
        self.store = store
        self.owner = owner
        self.default_api_id = default_api_id
        self.default_api_hash = default_api_hash
        self.state = None
        self.meta = {}
        self.client = None
        self.phone = None
        self.code_hash = None
        self.upload = None
        self.expires = 0
        self.runner = None
        self.expiry_task = None

    def options(self):
        return api_options(self.meta, self.default_api_id, self.default_api_hash)

    async def reset(self):
        if self.client:
            await self.client.disconnect()
        self.client = None
        self.state = None
        self.meta = {}
        self.phone = self.code_hash = self.upload = None

    def stage(self, value):
        self.state = value
        self.expires = time.monotonic() + 600

    async def post_init(self, app):
        async def notify(text):
            await app.bot.send_message(self.owner, text)
        async def reporter(job_id):
            file = io.BytesIO(report_csv(self.store, job_id))
            file.name = f"report-{job_id}.csv"
            await app.bot.send_document(self.owner, file, caption="Отчёт: чаты, статусы и причины ошибок.")
        self.runner = Runner(self.store, notify, reporter)
        self.expiry_task = asyncio.create_task(self.expire())

    async def expire(self):
        while True:
            await asyncio.sleep(30)
            if self.state and time.monotonic() > self.expires:
                await self.reset()

    async def post_stop(self, app):
        if self.expiry_task:
            self.expiry_task.cancel()
            await asyncio.gather(self.expiry_task, return_exceptions=True)
        if self.runner:
            await self.runner.shutdown()
        await self.reset()

    async def post_shutdown(self, app):
        self.store.close()

    async def delete_secret(self, message):
        try:
            await message.delete()
        except Exception:
            pass

    async def start(self, update, context):
        if not allowed(update, self.owner):
            if update.effective_chat.type == "private":
                await update.message.reply_text(
                    f"Ваш Telegram ID: {update.effective_user.id}. Доступ к панели закрыт.")
            return
        await self.reset()
        await update.message.reply_text(self.menu_text(), reply_markup=MENU)

    def menu_text(self):
        settings = self.store.settings()
        selected = sum(bool(a["selected"]) for a in self.store.accounts())
        ready = bool(settings["message"].strip() or settings.get("media"))
        return ("Панель аккаунтов и рассылок\n\n"
                f"1. Выбрано аккаунтов: {selected}\n"
                f"2. Сообщение: {'✅ сохранено' if ready else 'нужно добавить'}\n"
                f"3. Ссылок на чаты/папки: {len(settings['targets'])}\n"
                f"4. Циклов: {settings['cycles']}; между сообщениями: {settings['delay']} сек.; "
                f"между циклами: {settings['repeat']} сек.\n\n"
                "Нажмите нужный шаг, чтобы изменить его. Затем «Проверить и запустить».\n"
                "Используйте в чатах, где разрешены ваши объявления.")

    async def settings_menu(self, message):
        settings = self.store.settings()
        await message.reply_text(
            "Настройки рассылки\nВыберите параметр, который хотите изменить.\n"
            "Изменения действуют для новых планов.", reply_markup=keyboard([
                [(f"🔁 Количество циклов: {settings['cycles']}", "setting:cycles")],
                [(f"⏱ Между сообщениями: {settings['delay']} сек.", "setting:delay")],
                [(f"⏳ Между циклами: {settings['repeat']} сек.", "setting:repeat")],
                [("❔ Что означают параметры", "help:settings"), ("В меню", "menu")]]))

    async def setting_choices(self, message, field):
        if field not in SETTING_INFO:
            raise InputError("Неизвестный параметр")
        title, minimum, maximum, values = SETTING_INFO[field]
        rows = []
        for offset in range(0, len(values), 3):
            rows.append([(str(v) + (" сек." if field != "cycles" else ""), f"set:{field}:{v}")
                         for v in values[offset:offset + 3]])
        rows += [[("✏️ Своё число", f"custom:{field}")], [("← Настройки", "settings")]]
        await message.reply_text(
            f"{title}\nСейчас: {self.store.settings()[field]}. Выберите значение или введите своё.\n"
            f"Допустимо: {minimum}–{maximum}.", reply_markup=keyboard(rows))

    def set_number(self, field, text):
        if field not in SETTING_INFO or not re.fullmatch(r"[0-9]{1,8}", text):
            raise InputError("Нужно целое число")
        _, minimum, maximum, _ = SETTING_INFO[field]
        number = int(text)
        if not minimum <= number <= maximum:
            raise InputError(f"Укажите число от {minimum} до {maximum}")
        settings = self.store.settings()
        settings[field] = number
        self.store.set_settings(settings)

    async def help(self, update, context):
        if allowed(update, self.owner):
            await self.help_menu(update.effective_message)

    async def help_menu(self, message, topic=None):
        text = HELP_TEXT.get(topic, "Справка\nНастройте шаги 1–4, затем проверьте план и запустите его. "
                             "Ваши аккаунты и задачи видны только вам.\nВыберите тему:")
        await message.reply_text(text, reply_markup=keyboard([
            [("Аккаунты: JSON → .session", "help:account")],
            [("Сообщение", "help:message"), ("Чаты и addlist", "help:targets")],
            [("Циклы и паузы", "help:settings"), ("Запуск и отчёт", "help:report")],
            [("В меню", "menu")]]))

    async def identify(self, update, context):
        if update.effective_chat.type == "private":
            await update.message.reply_text(f"Ваш Telegram ID: {update.effective_user.id}")

    async def cancel(self, update, context):
        if allowed(update, self.owner):
            await self.reset()
            await update.message.reply_text("Ввод отменён.", reply_markup=MENU)

    async def stop(self, update, context):
        if allowed(update, self.owner):
            if self.runner.busy:
                self.runner.stop.set()
                text = "Останавливаю задачу после текущего запроса к Telegram."
            else:
                text = "Активной задачи нет."
            await update.effective_message.reply_text(text, reply_markup=MENU)

    async def status(self, update, context):
        if allowed(update, self.owner):
            jobs = self.store.jobs()
            labels = {"draft": "план готов", "running": "выполняется", "done": "завершена",
                      "stopped": "остановлена", "error": "прервана", "interrupted": "нужно продолжить после перезапуска"}
            text = "Последние задачи:\n" + "\n".join(
                f"{j['id']} — {labels.get(j['state'], j['state'])}\n{j['detail']}" for j in jobs)
            await update.effective_message.reply_text(text[:3900] if jobs else "Задач пока нет.", reply_markup=MENU)

    async def account_menu(self, message):
        accounts = self.store.accounts()
        rows = []
        for account in accounts:
            mark = "✅" if account["selected"] else "▫️"
            rows.append([(f"{mark} {account['label']}", f"toggle:{account['id']}"),
                         ("Удалить", f"delete:{account['id']}")])
        rows += [[("Добавить", "add"), ("В меню", "menu")]]
        await message.reply_text(
            "Выберите аккаунты для рассылки. ✅ — выбран.\n"
            "Если выбрано несколько, каждый отправит сообщение во все заданные чаты.",
            reply_markup=keyboard(rows))

    async def callback(self, update, context):
        query = update.callback_query
        if not allowed(update, self.owner):
            await query.answer("Нет доступа", show_alert=True)
            return
        await query.answer()
        action = query.data
        message = query.message
        try:
            if action == "menu":
                await self.reset()
                await message.reply_text(self.menu_text(), reply_markup=MENU)
            elif action in {"settings", "timing"}:
                await self.reset()
                await self.settings_menu(message)
            elif action.startswith("setting:"):
                await self.reset()
                await self.setting_choices(message, action.split(":")[1])
            elif action.startswith("set:"):
                _, field, value = action.split(":")
                self.set_number(field, value)
                await self.reset()
                await self.settings_menu(message)
            elif action.startswith("custom:"):
                field = action.split(":")[1]
                if field not in SETTING_INFO:
                    raise InputError("Неизвестный параметр")
                await self.reset()
                self.stage("setting:" + field)
                await message.reply_text(SETTING_INFO[field][0] + ": пришлите число.",
                                         reply_markup=keyboard([[("← Настройки", "settings")]]))
            elif action == "help" or action.startswith("help:"):
                await self.reset()
                await self.help_menu(message, action.split(":")[1] if ":" in action else None)
            elif action == "add":
                await self.reset()
                await message.reply_text("Выберите способ подключения своего аккаунта:", reply_markup=IMPORT_MENU)
            elif action == "accounts":
                await self.account_menu(message)
            elif action.startswith("toggle:"):
                self.store.toggle(int(action.split(":")[1]))
                await self.account_menu(message)
            elif action.startswith("delete:"):
                if self.runner.busy:
                    raise InputError("Сначала остановите выполняющуюся задачу")
                account_id = int(action.split(":")[1])
                await message.reply_text("Удалить аккаунт из панели? Вход в Telegram не будет отозван.",
                    reply_markup=keyboard([[("Удалить", f"remove:{account_id}"), ("Отмена", "accounts")]]))
            elif action.startswith("remove:"):
                if self.runner.busy:
                    raise InputError("Сначала остановите выполняющуюся задачу")
                self.store.delete_account(int(action.split(":")[1]))
                await self.account_menu(message)
            elif action.startswith("import:"):
                await self.reset()
                kind = action.split(":")[1]
                self.stage(kind)
                prompts = {
                    "phone": "Пришлите номер своего аккаунта в международном формате: +7...",
                    "json": "Пришлите JSON файлом. Если в нём только параметры приложения, затем понадобится .session или вход по номеру.",
                    "telethon": "Пришлите файл .session Telethon. Остановите другие программы, использующие эту сессию.",
                    "pyrogram": "Пришлите файл .session Pyrogram (production, личный аккаунт).",
                    "string": "Пришлите полную StringSession Telethon текстом.",
                    "tdata": "Пришлите ZIP с одной папкой TData (один аккаунт). До 18 МБ.",
                    "key": 'Пришлите JSON текстом с полным ключом:\n'
                           '{"dc_id": 1, "auth_key": "512 HEX-символов", "user_id": 123456789}\n'
                           'user_id необязателен. Номер телефона для входа по ключу не нужен.',
                }
                await message.reply_text(prompts[kind] + "\nОтмена: /cancel")
            elif action in {"message", "targets"}:
                await self.reset()
                self.stage(action)
                prompts = {
                    "message": "Пришлите текст объявления (до 4096 символов) либо одно фото/видео с подписью "
                               "(до 1024 символов). Фото/видео — до 18 МБ. Без форматирования.",
                    "targets": "Пришлите ссылки на чаты / addlist по одной на строку. Можно .txt файлом. Пример:\n"
                               "https://t.me/your_group\nhttps://t.me/+invite_hash\nhttps://t.me/addlist/folder_slug\n"
                               "При запуске аккаунт вступит в недостающие чаты. Для заявок нужно одобрение администратора.",
                    "timing": "Пришлите два целых числа через пробел:\n"
                              "пауза между действиями, пауза между циклами (секунды).\n"
                              "Пример: 30 0 — один цикл. 30 3600 — повторять через час после завершения цикла.\n"
                              "Пауза между действиями не менее 1 сек.",
                }
                await message.reply_text(prompts[action] + "\nОтмена: /cancel",
                                         reply_markup=keyboard([[("← В меню", "menu")]]))
            elif action == "preview":
                if self.runner.busy:
                    raise InputError("Сначала остановите выполняющуюся задачу")
                await self.reset()
                await self.preview(message)
            elif action.startswith(("run:", "resume:")):
                if self.runner.busy:
                    raise InputError("Уже выполняется задача")
                job_id = action.split(":")[1]
                job = self.store.job(job_id)
                if action.startswith("run:") and job["state"] != "draft":
                    raise InputError("Этот план уже запускался. Используйте статус или создайте новый")
                await message.reply_text(f"Запускаю задачу {job_id}. Остановка: /stop")
                self.runner.launch(job_id)
            elif action == "status":
                await self.status(update, context)
                rows = []
                for job in self.store.jobs():
                    row = [(f"Журнал {job['id']}", f"journal:{job['id']}")]
                    if job["state"] in {"interrupted", "stopped", "error"}:
                        row.append(("Продолжить", f"resume:{job['id']}"))
                    rows.append(row)
                if rows:
                    await message.reply_text(
                        "Продолжение пропустит sent и pending. Сначала проверьте pending в журнале: "
                        "после сетевого сбоя сообщение могло быть отправлено.", reply_markup=keyboard(rows))
            elif action == "stop":
                await self.stop(update, context)
            elif action == "journal":
                await self.journal(message)
            elif action.startswith("journal:"):
                await self.journal(message, action.split(":")[1])
        except Exception as exc:
            await message.reply_text(error_text(exc), reply_markup=MENU)

    async def begin_phone(self, phone, message):
        if not re.fullmatch(r"\+[1-9][0-9]{6,14}", phone):
            raise InputError("Номер нужен в формате +код_страны_номер, без пробелов")
        self.client = make_client(StringSession(), self.options())
        await self.client.connect()
        sent = await self.client.send_code_request(phone)
        self.phone, self.code_hash = phone, sent.phone_code_hash
        self.stage("code")
        await message.reply_text("Запрос кода принят Telegram. Тип доставки: " + type(sent.type).__name__ +
                                 ".\nПришлите полученный код. Отмена: /cancel")

    async def finish(self, client, message, options=None, expected=None):
        user_id, label, payload = await validate(client, options, expected)
        self.store.save_account(user_id, label, payload)
        await self.reset()
        await message.reply_text(f"Подключён аккаунт: {label}", reply_markup=MENU)

    async def load_json(self, meta, message):
        self.meta = meta
        options = self.options()
        expected = meta.get("user_id") or meta.get("id")
        session = meta.get("session_string") or meta.get("string_session")
        key = meta.get("auth_key") or meta.get("auth_key_hex")
        if session:
            await self.finish(make_client(StringSession(session), options), message, options, expected)
        elif key:
            client = await from_key(key, meta["dc_id"], options)
            await self.finish(client, message, options, expected)
        else:
            self.stage("json_next")
            await message.reply_text("Параметры приложения прочитаны. В JSON нет ключа авторизации.\n"
                                     "Теперь пришлите .session (Telethon/Pyrogram) либо номер телефона для входа.")

    async def content(self, update, context):
        if not allowed(update, self.owner):
            return
        message = update.message
        if not self.state:
            await message.reply_text("Выберите действие в меню.", reply_markup=MENU)
            return
        if time.monotonic() > self.expires:
            await self.reset()
            await message.reply_text("Время ввода истекло. Начните заново.", reply_markup=MENU)
            return
        stage = self.state
        text = message.text or ""
        data = None
        try:
            if message.document and stage != "message":
                if not message.document.file_size or message.document.file_size > MAX_UPLOAD:
                    raise InputError("Размер файла должен быть от 1 байта до 18 МБ")
                file = await message.document.get_file()
                data = bytes(await file.download_as_bytearray())
                if len(data) > MAX_UPLOAD:
                    raise InputError("Файл слишком большой")
            if stage not in {"message", "targets", "timing"} and not stage.startswith("setting:"):
                await self.delete_secret(message)
            if stage.startswith("setting:"):
                self.set_number(stage.split(":")[1], text.strip())
                await self.reset()
                await self.settings_menu(message)
            elif stage == "message":
                photo = message.photo[-1] if message.photo else None
                video = message.video
                document = message.document
                attachment = photo or video
                kind = "photo" if photo else "video" if video else None
                if not attachment and document:
                    mime = document.mime_type or ""
                    if mime in {"image/jpeg", "image/png"}:
                        attachment, kind = document, "photo"
                    elif mime.startswith("video/"):
                        attachment, kind = document, "video"
                if message.media_group_id:
                    raise InputError("Пришлите одно фото или одно видео с подписью, без альбома")
                if attachment:
                    text = message.caption or ""
                    if len(text.encode("utf-16-le")) // 2 > 1024:
                        raise InputError("Подпись к фото/видео должна быть до 1024 символов Telegram")
                    if not attachment.file_size or attachment.file_size > MAX_UPLOAD:
                        raise InputError("Фото/видео должно быть размером до 18 МБ")
                elif not text.strip() or len(text.encode("utf-16-le")) // 2 > 4096:
                    raise InputError("Пришлите текст до 4096 символов либо фото/видео с подписью")
                settings = self.store.settings()
                settings["message"] = text
                settings["media"] = None
                if attachment:
                    file = await attachment.get_file()
                    data = bytes(await file.download_as_bytearray())
                    if len(data) > MAX_UPLOAD:
                        raise InputError("Фото/видео должно быть размером до 18 МБ")
                    suffix = Path(getattr(attachment, "file_name", "") or "").suffix.lower()
                    settings["media"] = {
                        "id": self.store.save_media(data), "kind": kind,
                        "name": "attachment" + (suffix or (".jpg" if kind == "photo" else ".mp4")),
                        "mime_type": getattr(attachment, "mime_type", None),
                        "width": getattr(attachment, "width", 0),
                        "height": getattr(attachment, "height", 0),
                        "duration": getattr(attachment, "duration", 0),
                    }
                self.store.set_settings(settings)
                await self.reset()
                await message.reply_text("Сообщение с вложением сохранено." if attachment else "Текст сохранён.",
                                         reply_markup=MENU)
            elif stage == "targets":
                targets = parse_targets(data.decode("utf-8-sig") if data else text)
                settings = self.store.settings()
                settings["targets"] = targets
                self.store.set_settings(settings)
                await self.reset()
                await message.reply_text(f"Сохранено исходных ссылок: {len(targets)}", reply_markup=MENU)
            elif stage == "timing":
                values = text.split()
                if len(values) != 2 or not all(re.fullmatch(r"[0-9]{1,7}", v) for v in values):
                    raise InputError("Нужны два целых числа: например 30 0")
                delay, repeat = map(int, values)
                if not 1 <= delay <= 86400 or not 0 <= repeat <= 2592000:
                    raise InputError("Пауза: 1–86400 сек., повторение: 0–2592000 сек.")
                settings = self.store.settings()
                settings.update(delay=delay, repeat=repeat)
                self.store.set_settings(settings)
                await self.reset()
                await message.reply_text("Паузы сохранены.", reply_markup=MENU)
            elif stage in {"phone", "json_next"} and data is None:
                await self.begin_phone(text.strip(), message)
            elif stage == "code":
                if not re.fullmatch(r"[0-9]{4,8}", text.strip()):
                    raise InputError("Пришлите код цифрами")
                try:
                    await self.client.sign_in(self.phone, text.strip(), phone_code_hash=self.code_hash)
                except errors.SessionPasswordNeededError:
                    self.stage("password")
                    await message.reply_text("У аккаунта включена 2FA. Пришлите пароль. Отмена: /cancel")
                    return
                await self.finish(self.client, message, self.options())
            elif stage == "password":
                await self.client.sign_in(password=text)
                await self.finish(self.client, message, self.options())
            elif stage in {"json", "key"}:
                await self.load_json(parse_json(data if data is not None else text), message)
            elif stage == "string":
                options = self.options()
                await self.finish(make_client(StringSession(text.strip()), options), message, options)
            elif stage in {"telethon", "pyrogram", "json_next"}:
                if data is None:
                    raise InputError("Пришлите .session файлом")
                kind = stage
                if kind == "json_next":
                    # Both session formats have sessions; Pyrogram has test_mode.
                    import sqlite3
                    db = sqlite3.connect(":memory:")
                    try:
                        db.deserialize(data)
                        cols = {r[1] for r in db.execute("PRAGMA table_info(sessions)")}
                    finally:
                        db.close()
                    kind = "pyrogram" if "test_mode" in cols else "telethon"
                options = self.options()
                client = await from_sqlite(data, kind, options)
                await self.finish(client, message, options, self.meta.get("id") or self.meta.get("user_id"))
            elif stage == "tdata":
                if data is None:
                    raise InputError("Пришлите ZIP файлом")
                self.upload = data
                self.stage("tdata_password")
                await message.reply_text("Пришлите локальный пароль TData. Если пароля нет — отправьте один символ: -")
            elif stage == "tdata_password":
                client = await from_tdata(self.upload, "" if text == "-" else text)
                await self.finish(client, message)
            else:
                raise InputError("Неподходящий тип сообщения")
        except Exception as exc:
            retry = isinstance(exc, (errors.PhoneCodeInvalidError, errors.PasswordHashInvalidError, InputError))
            if not retry:
                await self.reset()
            await message.reply_text(error_text(exc) + "\nОтмена: /cancel", reply_markup=MENU if not retry else None)

    async def preview(self, message):
        settings = self.store.settings()
        accounts = [a for a in self.store.accounts() if a["selected"]]
        if not accounts or not (settings["message"].strip() or settings.get("media")) or not settings["targets"]:
            raise InputError("Нужны выбранный аккаунт, текст сообщения и ссылки")
        await message.reply_text("Проверяю ссылки и собираю план. Вступлений и отправок пока нет.")
        snapshot = {**settings, "accounts": []}
        lines = []
        total, joins, issue_count = 0, 0, 0
        for account in accounts:
            client = None
            issues = []
            items = []
            try:
                payload = self.store.account(account["id"])
                client = make_client(StringSession(payload["session"]), payload["options"])
                await client.connect()
                if not await client.is_user_authorized():
                    raise InputError(f"Аккаунт {account['id']}: сессия отозвана")
                items = await prepare(client, settings["targets"], issues)
            except errors.FloodWaitError:
                raise
            except Exception as exc:
                issues.append({"id": "account:" + str(account["id"]), "title": account["label"],
                               "source": "", "reason": error_text(exc)})
            finally:
                if client:
                    await client.disconnect()
            snapshot["accounts"].append({"account_id": account["id"], "items": items, "issues": issues})
            total += len(items)
            joins += sum(not i["joined"] for i in items)
            issue_count += len(issues)
            lines.append(account["label"] + ":")
            lines.extend(f"  {'Уже состоит' if i['joined'] else 'Вступить'} — {i['title']}" for i in items)
            lines.extend(f"  ⚠ {i['title']} — {i['reason']}" for i in issues)
        job_id = secrets.token_hex(4)
        self.store.new_job(job_id, snapshot)
        report = "\n".join(lines)
        if len(report) > 2400:
            output = io.BytesIO(report.encode("utf-8"))
            output.name = "plan.txt"
            await message.reply_document(output, caption="Полный список получателей")
            report = "Полный список приложен файлом."
        await message.reply_text(
            f"План {job_id}\n{report}\n\nОтправок за цикл: {total}. Вступлений: до {joins}.\n"
            f"Ошибок проверки: {issue_count} (будут в отчёте, остальные чаты продолжат работу).\n"
            f"Циклов: {settings['cycles']}. Всего запланировано отправок: {total * settings['cycles']}.\n"
            f"Между сообщениями: {settings['delay']} сек.\n"
            f"Между циклами: {settings['repeat']} сек.\n\n"
            f"Текст:\n{settings['message'][:700]}\n\n"
            + (f"Вложение: {'фото' if settings['media']['kind'] == 'photo' else 'видео'}\n\n"
               if settings.get("media") else "") +
            "Нажатие «Запустить» выполнит вступления и рассылку из этого плана.",
            reply_markup=keyboard([[("Запустить", f"run:{job_id}"), ("В меню", "menu")]]))

    async def journal(self, message, job_id=None):
        jobs = self.store.jobs()
        if not jobs:
            raise InputError("Журнал пуст")
        job_id = job_id or jobs[0]["id"]
        self.store.job(job_id)
        file = io.BytesIO(report_csv(self.store, job_id))
        file.name = f"journal-{job_id}.csv"
        await message.reply_document(file, caption="sent — отправлено; failed — не отправлено; pending — проверьте вручную.")

    async def on_error(self, update, context):
        # Deliberately omit update content, request objects, tokens and traceback.
        logging.getLogger("panel").error("Handler failed: %s", type(context.error).__name__)


def migrate_legacy(data_dir, legacy_owner):
    """Copy the previous private workspace to its owner; never give it to a new visitor."""
    data_dir = Path(data_dir)
    source = data_dir / "bot.sqlite3"
    marker = data_dir / "legacy-migrated.json"
    if not source.exists() or marker.exists():
        return
    try:
        user_id = int(legacy_owner or 0)
    except ValueError:
        user_id = 0
    if user_id <= 0:
        raise InputError("Для переноса прежних данных оставьте прежний OWNER_ID на первый запуск. "
                         "После переноса переменную можно удалить; она не ограничивает доступ")
    destination = data_dir / "users" / str(user_id) / "bot.sqlite3"
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        with closing(sqlite3.connect(destination)) as db:
            try:
                row = db.execute("SELECT owner FROM legacy_import WHERE id=1").fetchone()
            except sqlite3.OperationalError:
                row = None
        if not row or row[0] != user_id:
            raise InputError("Перенос остановлен: у прежнего владельца уже есть отдельное хранилище. "
                             "Существующие данные не перезаписаны")
    else:
        temporary = destination.with_suffix(".importing")
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as src:
            with closing(sqlite3.connect(temporary)) as dst:
                src.backup(dst)
                dst.execute("CREATE TABLE IF NOT EXISTS legacy_import(id INTEGER PRIMARY KEY,owner INTEGER)")
                dst.execute("INSERT OR REPLACE INTO legacy_import VALUES(1,?)", (user_id,))
                dst.commit()
        temporary.replace(destination)
    marker.write_text(json.dumps({"user_id": user_id}), encoding="utf-8")


class PerUserProcessor(BaseUpdateProcessor):
    """Keep each user's input ordered while other users can work independently."""
    def __init__(self):
        super().__init__(16)
        self.locks = {}

    async def initialize(self):
        pass

    async def shutdown(self):
        self.locks.clear()

    async def do_process_update(self, update, coroutine):
        user = getattr(update, "effective_user", None)
        key = user.id if user else None
        async with self.locks.setdefault(key, asyncio.Lock()):
            await coroutine


class PublicPanel:
    def __init__(self, root, key, default_id, default_hash):
        self.root = Path(root).resolve()
        self.key = key
        self.default_id = default_id
        self.default_hash = default_hash
        self.panels = {}

    async def post_init(self, app):
        await app.bot.set_my_commands([
            ("start", "Меню"), ("id", "Мой Telegram ID"),
            ("cancel", "Отменить ввод"), ("stop", "Остановить рассылку"),
            ("status", "Статус задач"), ("help", "Справка по использованию")])

    async def user_panel(self, user_id, app):
        if user_id not in self.panels:
            store = Store(self.root / "users" / str(user_id), self.key)
            panel = Panel(store, user_id, self.default_id, self.default_hash)
            self.panels[user_id] = panel
            await panel.post_init(app)
        return self.panels[user_id]

    def route(self, method):
        async def dispatch(update, context):
            if (not update.effective_user or not update.effective_chat
                    or update.effective_chat.type != "private"):
                if update.callback_query:
                    await update.callback_query.answer("Откройте личный чат с ботом")
                return
            panel = await self.user_panel(update.effective_user.id, context.application)
            await getattr(panel, method)(update, context)
        return dispatch

    async def post_stop(self, app):
        await asyncio.gather(*(panel.post_stop(app) for panel in self.panels.values()))

    async def post_shutdown(self, app):
        for panel in self.panels.values():
            panel.store.close()
        self.panels.clear()

    async def on_error(self, update, context):
        logging.getLogger("panel").error("Handler failed: %s", type(context.error).__name__)


def build_app():
    load_dotenv(ROOT / ".env")
    token = os.environ.get("BOT_TOKEN", "")
    key = os.environ.get("ENCRYPTION_KEY", "")
    if not token or not key:
        raise InputError("Заполните BOT_TOKEN и ENCRYPTION_KEY в .env или настройках BotHost")
    try:
        default_id = int(os.environ.get("TELEGRAM_API_ID", "0"))
    except ValueError:
        raise InputError("TELEGRAM_API_ID должен быть числом") from None
    default_hash = os.environ.get("TELEGRAM_API_HASH", "")
    data_dir = Path(os.environ.get("DATA_DIR", str(ROOT / "data"))).resolve()
    # Validate the encryption key even before the first visitor creates a workspace.
    from cryptography.fernet import Fernet
    Fernet(key.encode())
    migrate_legacy(data_dir, os.environ.get("OWNER_ID"))
    panel = PublicPanel(data_dir, key, default_id, default_hash)
    app = (Application.builder().token(token).concurrent_updates(PerUserProcessor())
           .post_init(panel.post_init).post_stop(panel.post_stop)
           .post_shutdown(panel.post_shutdown).build())
    app.add_handler(CommandHandler("start", panel.route("start")))
    app.add_handler(CommandHandler("id", panel.route("identify")))
    app.add_handler(CommandHandler("cancel", panel.route("cancel")))
    app.add_handler(CommandHandler("stop", panel.route("stop")))
    app.add_handler(CommandHandler("status", panel.route("status")))
    app.add_handler(CommandHandler("help", panel.route("help")))
    app.add_handler(CallbackQueryHandler(panel.route("callback")))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE &
                                   (filters.TEXT | filters.Document.ALL | filters.PHOTO | filters.VIDEO)
                                   & ~filters.COMMAND, panel.route("content")))
    app.add_error_handler(panel.on_error)
    return app


if __name__ == "__main__":
    logging.basicConfig(level=logging.ERROR, format="%(asctime)s %(name)s %(message)s")
    try:
        build_app().run_polling(drop_pending_updates=True)
    except Exception as error:
        print("Ошибка запуска:", error_text(error))
        raise SystemExit(1)

"""Private Telegram control panel. Entry point for PyCharm and BotHost."""
import asyncio
import csv
import io
import json
import logging
import os
from pathlib import Path
import re
import secrets
import time

from dotenv import load_dotenv
from telegram import InlineKeyboardButton as Button, InlineKeyboardMarkup as Keyboard
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters
from telethon import errors
from telethon.sessions import StringSession

from accounts import (InputError, api_options, error_text, from_key, from_sqlite,
                      from_tdata, make_client, parse_json, validate)
from delivery import Runner, parse_targets, prepare
from storage import Store

ROOT = Path(__file__).resolve().parent
MAX_UPLOAD = 18 * 1024 * 1024


def keyboard(rows):
    return Keyboard([[Button(label, callback_data=value) for label, value in row] for row in rows])


MENU = keyboard([
    [("Аккаунты", "accounts"), ("Добавить аккаунт", "add")],
    [("Текст сообщения", "message"), ("Ссылки / addlist", "targets")],
    [("Паузы и повторение", "timing"), ("Создать план", "preview")],
    [("Статус", "status"), ("Остановить", "stop")],
    [("Журнал CSV", "journal")],
])

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
        self.runner = Runner(self.store, notify)
        self.expiry_task = asyncio.create_task(self.expire())
        await app.bot.set_my_commands([
            ("start", "Меню"), ("id", "Мой Telegram ID"),
            ("cancel", "Отменить ввод"), ("stop", "Остановить рассылку"),
            ("status", "Статус задач")])

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
        await update.message.reply_text(
            "Панель аккаунтов и рассылок.\n"
            "1. Добавьте свой аккаунт.\n2. Задайте текст и ссылки на чаты.\n"
            "3. Создайте план и запустите его.\n"
            "Используйте в чатах, где разрешены ваши объявления.", reply_markup=MENU)

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
            text = "Последние задачи:\n" + "\n".join(
                f"{j['id']} — {j['state']}\n{j['detail']}" for j in jobs)
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
                await message.reply_text("Главное меню", reply_markup=MENU)
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
            elif action in {"message", "targets", "timing"}:
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
                await message.reply_text(prompts[action] + "\nОтмена: /cancel")
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
            if stage not in {"message", "targets", "timing"}:
                await self.delete_secret(message)
            if stage == "message":
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
        total, joins = 0, 0
        for account in accounts:
            payload = self.store.account(account["id"])
            client = make_client(StringSession(payload["session"]), payload["options"])
            try:
                await client.connect()
                if not await client.is_user_authorized():
                    raise InputError(f"Аккаунт {account['id']}: сессия отозвана")
                items = await prepare(client, settings["targets"])
                if not items:
                    raise InputError("Получился пустой список чатов")
                snapshot["accounts"].append({"account_id": account["id"], "items": items})
                total += len(items)
                joins += sum(not i["joined"] for i in items)
                lines.append(account["label"] + ":")
                lines.extend(f"  {'Уже состоит' if i['joined'] else 'Вступить'} — {i['title']}" for i in items)
            finally:
                await client.disconnect()
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
            f"Пауза между действиями: {settings['delay']} сек.\n"
            f"Повторение: {str(settings['repeat']) + ' сек. после цикла' if settings['repeat'] else 'один цикл'}.\n\n"
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
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(["job", "cycle", "account", "chat", "status"])
        for row in self.store.deliveries(job_id):
            writer.writerow([row[k] for k in ("job", "cycle", "account", "chat", "status")])
        file = io.BytesIO(output.getvalue().encode("utf-8-sig"))
        file.name = f"journal-{job_id}.csv"
        await message.reply_document(file, caption="sent — отправлено; pending — результат требует проверки в Telegram.")

    async def on_error(self, update, context):
        # Deliberately omit update content, request objects, tokens and traceback.
        logging.getLogger("panel").error("Handler failed: %s", type(context.error).__name__)


def build_app():
    load_dotenv(ROOT / ".env")
    token = os.environ.get("BOT_TOKEN", "")
    key = os.environ.get("ENCRYPTION_KEY", "")
    if not token or not key:
        raise InputError("Заполните BOT_TOKEN и ENCRYPTION_KEY в .env или настройках BotHost")
    try:
        owner = int(os.environ.get("OWNER_ID", "0"))
        default_id = int(os.environ.get("TELEGRAM_API_ID", "0"))
    except ValueError:
        raise InputError("OWNER_ID и TELEGRAM_API_ID должны быть числами") from None
    default_hash = os.environ.get("TELEGRAM_API_HASH", "")
    data_dir = Path(os.environ.get("DATA_DIR", str(ROOT / "data")))
    store = Store(data_dir, key)
    panel = Panel(store, owner, default_id, default_hash)
    app = (Application.builder().token(token).concurrent_updates(False)
           .post_init(panel.post_init).post_stop(panel.post_stop)
           .post_shutdown(panel.post_shutdown).build())
    app.add_handler(CommandHandler("start", panel.start))
    app.add_handler(CommandHandler("id", panel.identify))
    app.add_handler(CommandHandler("cancel", panel.cancel))
    app.add_handler(CommandHandler("stop", panel.stop))
    app.add_handler(CommandHandler("status", panel.status))
    app.add_handler(CallbackQueryHandler(panel.callback))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE &
                                   (filters.TEXT | filters.Document.ALL | filters.PHOTO | filters.VIDEO)
                                   & ~filters.COMMAND, panel.content))
    app.add_error_handler(panel.on_error)
    return app


if __name__ == "__main__":
    logging.basicConfig(level=logging.ERROR, format="%(asctime)s %(name)s %(message)s")
    try:
        build_app().run_polling(drop_pending_updates=True)
    except Exception as error:
        print("Ошибка запуска:", error_text(error))
        raise SystemExit(1)

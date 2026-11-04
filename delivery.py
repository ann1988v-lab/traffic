"""Read-only planning, then explicit join/send execution of the saved plan."""
import asyncio
import re
from urllib.parse import urlparse

from telethon import errors, functions, types, utils
from telethon.sessions import StringSession

from accounts import InputError, error_text, make_client


def parse_target(value):
    value = value.strip()
    if value.startswith("@") and re.fullmatch(r"@[A-Za-z][A-Za-z0-9_]{3,31}", value):
        return "public", value[1:]
    parsed = urlparse(value)
    if parsed.scheme != "https" or parsed.netloc not in {"t.me", "telegram.me"}:
        raise InputError("Нужны @username либо ссылки https://t.me/...")
    parts = parsed.path.strip("/").split("/")
    if len(parts) == 2 and parts[0] == "addlist" and re.fullmatch(r"[\w-]+", parts[1]):
        return "folder", parts[1]
    if len(parts) == 1 and re.fullmatch(r"\+[A-Za-z0-9_-]+", parts[0]):
        return "private", parts[0][1:]
    if len(parts) == 2 and parts[0] == "joinchat" and re.fullmatch(r"[A-Za-z0-9_-]+", parts[1]):
        return "private", parts[1]
    if len(parts) == 1 and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", parts[0]):
        return "public", parts[0]
    raise InputError("Неизвестный формат ссылки. Ссылки на сообщения не подходят")


def parse_targets(text):
    result = []
    seen = set()
    for line in text.splitlines():
        if not line.strip():
            continue
        kind, value = parse_target(line)
        if (kind, value.lower() if kind == "public" else value) not in seen:
            result.append(line.strip())
            seen.add((kind, value.lower() if kind == "public" else value))
    if not result:
        raise InputError("Список чатов пуст")
    if len(result) > 300:
        raise InputError("За один план поддерживается до 300 исходных ссылок")
    return result


def pack(entity, joined, source, kind, value):
    if not isinstance(entity, (types.Channel, types.Chat)):
        raise InputError("Ссылка ведёт на личный аккаунт, а не чат")
    if getattr(entity, "deactivated", False):
        raise InputError("Чат деактивирован; используйте ссылку на действующую группу")
    return {"id": utils.get_peer_id(entity), "raw_id": entity.id,
            "hash": getattr(entity, "access_hash", 0),
            "channel": isinstance(entity, types.Channel),
            "title": entity.title, "joined": joined, "source": source,
            "kind": kind, "value": value}


def input_peer(item):
    if item["channel"]:
        return types.InputPeerChannel(item["raw_id"], item["hash"])
    return types.InputPeerChat(item["raw_id"])


async def prepare(client, targets):
    dialogs = {utils.get_peer_id(d.entity) async for d in client.iter_dialogs()}
    result = []
    for source in targets:
        kind, value = parse_target(source)
        if kind == "folder":
            invite = await client(functions.chatlists.CheckChatlistInviteRequest(value))
            peers = (list(invite.peers) if hasattr(invite, "peers") else
                     list(invite.missing_peers) + list(invite.already_peers))
            by_id = {utils.get_peer_id(c): c for c in invite.chats}
            for peer in peers:
                chat_id = utils.get_peer_id(peer)
                if chat_id not in by_id:
                    raise InputError("Telegram не вернул данные одного из чатов папки")
                result.append(pack(by_id[chat_id], chat_id in dialogs, source, kind, value))
        elif kind == "private":
            invite = await client(functions.messages.CheckChatInviteRequest(value))
            if isinstance(invite, types.ChatInviteAlready):
                result.append(pack(invite.chat, True, source, kind, value))
            else:
                # The actual ID is not available until an ordinary private invite is joined.
                result.append({"id": "invite:" + value, "title": invite.title,
                               "joined": False, "source": source, "kind": kind, "value": value})
        else:
            entity = await client.get_entity(value)
            result.append(pack(entity, utils.get_peer_id(entity) in dialogs, source, kind, value))
    unique = {}
    for item in result:
        unique.setdefault(str(item["id"]), item)
    return list(unique.values())


async def wait_or_stop(stop, seconds):
    if stop.is_set():
        raise asyncio.CancelledError()
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        return
    raise asyncio.CancelledError()


async def ensure_joined(client, items, stop, delay):
    """Join exactly the chats included in the approved snapshot."""
    dialogs = {utils.get_peer_id(d.entity) async for d in client.iter_dialogs()}
    items = [{**item, "joined": item["id"] in dialogs} for item in items]
    folders = {}
    for item in items:
        if item["kind"] == "folder" and not item["joined"]:
            folders.setdefault(item["value"], []).append(item)
    for slug, missing in folders.items():
        if stop.is_set():
            raise asyncio.CancelledError()
        await client(functions.chatlists.JoinChatlistInviteRequest(
            slug=slug, peers=[input_peer(i) for i in missing]))
        await wait_or_stop(stop, delay)
    resolved = []
    for original in items:
        item = dict(original)
        if stop.is_set():
            raise asyncio.CancelledError()
        if not item["joined"] and item["kind"] == "public":
            try:
                await client(functions.channels.JoinChannelRequest(input_peer(item)))
            except errors.UserAlreadyParticipantError:
                pass
            await wait_or_stop(stop, delay)
        elif not item["joined"] and item["kind"] == "private":
            try:
                updates = await client(functions.messages.ImportChatInviteRequest(item["value"]))
                if not updates.chats:
                    raise InputError("После вступления Telegram не вернул чат")
                entity = updates.chats[0]
            except errors.UserAlreadyParticipantError:
                invite = await client(functions.messages.CheckChatInviteRequest(item["value"]))
                if not isinstance(invite, types.ChatInviteAlready):
                    raise InputError("Не удалось определить приватный чат")
                entity = invite.chat
            item = pack(entity, True, item["source"], item["kind"], item["value"])
            await wait_or_stop(stop, delay)
        resolved.append(item)
    unique = {}
    for item in resolved:
        unique.setdefault(str(item["id"]), item)
    return list(unique.values())


class Runner:
    def __init__(self, store, notify):
        self.store = store
        self.notify = notify
        self.task = None
        self.stop = asyncio.Event()
        self.job_id = None

    @property
    def busy(self):
        return self.task is not None and not self.task.done()

    def launch(self, job_id):
        if self.busy:
            raise InputError("Уже выполняется задача. Сначала остановите её")
        if self.store.job(job_id)["state"] == "done":
            raise InputError("Эта задача уже завершена. Создайте новый план")
        self.stop = asyncio.Event()
        self.job_id = job_id
        self.store.state(job_id, "running")
        self.task = asyncio.create_task(self.run(job_id))

    async def run(self, job_id):
        total = 0
        try:
            snapshot = self.store.job(job_id)["snapshot"]
            cycle = 0
            while True:
                for account_plan in snapshot["accounts"]:
                    if self.stop.is_set():
                        raise asyncio.CancelledError()
                    account_id = account_plan["account_id"]
                    payload = self.store.account(account_id)
                    client = make_client(StringSession(payload["session"]), payload["options"])
                    try:
                        await client.connect()
                        if not await client.is_user_authorized():
                            raise InputError("Сессия аккаунта отозвана. Переподключите аккаунт")
                        items = await ensure_joined(client, account_plan["items"], self.stop,
                                                    snapshot["delay"])
                        for item in items:
                            if self.stop.is_set():
                                raise asyncio.CancelledError()
                            if not self.store.claim(job_id, cycle, account_id, item["id"]):
                                continue
                            await client.send_message(input_peer(item), snapshot["message"],
                                                      parse_mode=None, link_preview=False)
                            self.store.sent(job_id, cycle, account_id, item["id"])
                            total += 1
                            self.store.state(job_id, "running", f"За текущий запуск отправлено: {total}")
                            await wait_or_stop(self.stop, snapshot["delay"])
                    finally:
                        await client.disconnect()
                if not snapshot["repeat"]:
                    self.store.state(job_id, "done", f"За текущий запуск отправлено: {total}")
                    await self.safe_notify(f"Задача {job_id} завершена. Отправлено за запуск: {total}")
                    return
                self.store.state(job_id, "running", f"Отправлено за запуск: {total}. Ожидание следующего цикла")
                await wait_or_stop(self.stop, snapshot["repeat"])
                cycle += 1
        except asyncio.CancelledError:
            self.store.state(job_id, "stopped", f"Остановлено. За запуск отправлено: {total}")
            await self.safe_notify(f"Задача {job_id} остановлена")
        except Exception as exc:
            detail = error_text(exc)
            self.store.state(job_id, "error", detail)
            await self.safe_notify(f"Задача {job_id} остановлена: {detail}")

    async def safe_notify(self, text):
        try:
            await self.notify(text)
        except Exception:
            pass  # Progress is persisted; a Bot API outage must not trigger a resend.

    async def shutdown(self):
        if self.busy:
            self.stop.set()
            try:
                await asyncio.wait_for(asyncio.shield(self.task), timeout=25)
            except asyncio.TimeoutError:
                self.task.cancel()
                await asyncio.gather(self.task, return_exceptions=True)

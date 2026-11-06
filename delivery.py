"""Read-only planning, then explicit join/send execution of the saved plan."""
import asyncio
import csv
import io
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
    lines = []
    for line in text.splitlines():
        links = re.findall(r"https://(?:t\.me|telegram\.me)/[^\s<>\[\]()]+", line)
        lines.extend(links or [line])
    for line in lines:
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
    if not isinstance(entity, (types.Channel, types.Chat, types.ChannelForbidden, types.ChatForbidden)):
        raise InputError("Ссылка ведёт на личный аккаунт, а не чат")
    if getattr(entity, "deactivated", False):
        raise InputError("Чат деактивирован; используйте ссылку на действующую группу")
    return {"id": utils.get_peer_id(entity), "raw_id": entity.id,
            "hash": getattr(entity, "access_hash", 0),
            "channel": isinstance(entity, (types.Channel, types.ChannelForbidden)),
            "title": entity.title, "joined": joined, "source": source,
            "kind": kind, "value": value}


def input_peer(item):
    if item["channel"]:
        return types.InputPeerChannel(item["raw_id"], item["hash"])
    return types.InputPeerChat(item["raw_id"])


async def prepare(client, targets, issues=None):
    dialogs = {utils.get_peer_id(d.entity): d.entity async for d in client.iter_dialogs()}
    issues = issues if issues is not None else []
    result = []
    def issue(source, exc, chat_id=None, title=None):
        issues.append({"id": str(chat_id) if chat_id is not None else "source:" + source,
                       "title": title or source, "source": source, "reason": error_text(exc)})
    for source in targets:
        try:
            kind, value = parse_target(source)
            if kind == "folder":
                invite = await client(functions.chatlists.CheckChatlistInviteRequest(value))
                peers = (list(invite.peers) if hasattr(invite, "peers") else
                         list(invite.missing_peers) + list(invite.already_peers))
                by_id = {utils.get_peer_id(c): c for c in invite.chats}
                if not peers:
                    raise InputError("В папке нет доступных чатов")
                for peer in peers:
                    chat_id = utils.get_peer_id(peer)
                    entity = dialogs.get(chat_id) or by_id.get(chat_id)
                    try:
                        if isinstance(peer, types.PeerUser):
                            raise InputError("В папке указан личный аккаунт, он пропущен")
                        if entity is None:
                            entity = await client.get_entity(peer)
                        if utils.get_peer_id(entity) != chat_id:
                            raise InputError("Telegram не вернул данные нужного чата")
                        result.append(pack(entity, chat_id in dialogs, source, kind, value))
                    except errors.FloodWaitError:
                        raise
                    except Exception as exc:
                        issue(source, exc, chat_id, getattr(entity, "title", None))
            elif kind == "private":
                invite = await client(functions.messages.CheckChatInviteRequest(value))
                if isinstance(invite, types.ChatInviteAlready):
                    entity = dialogs.get(utils.get_peer_id(invite.chat)) or invite.chat
                    result.append(pack(entity, True, source, kind, value))
                else:
                    result.append({"id": "invite:" + value, "title": invite.title,
                                   "joined": False, "source": source, "kind": kind, "value": value})
            else:
                entity = await client.get_entity(value)
                result.append(pack(entity, utils.get_peer_id(entity) in dialogs, source, kind, value))
        except errors.FloodWaitError:
            raise
        except Exception as exc:
            issue(source, exc)
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


async def ensure_joined(client, items, stop, delay, issues=None, rpc=None):
    """Join exactly the chats included in the approved snapshot."""
    dialogs = {utils.get_peer_id(d.entity) async for d in client.iter_dialogs()}
    items = [{**item, "joined": item["id"] in dialogs} for item in items]
    issues = issues if issues is not None else []
    async def call(request):
        return await rpc(lambda: client(request)) if rpc else await client(request)
    resolved = []
    for original in items:
        item = dict(original)
        if stop.is_set():
            raise asyncio.CancelledError()
        try:
            if not item["joined"]:
                try:
                    if item["kind"] == "folder":
                        await call(functions.chatlists.JoinChatlistInviteRequest(
                            slug=item["value"], peers=[input_peer(item)]))
                    elif item["kind"] == "public":
                        await call(functions.channels.JoinChannelRequest(input_peer(item)))
                    else:
                        updates = await call(functions.messages.ImportChatInviteRequest(item["value"]))
                        if not updates.chats:
                            raise InputError("После вступления Telegram не вернул чат")
                        item = pack(updates.chats[0], True, item["source"], item["kind"], item["value"])
                except errors.UserAlreadyParticipantError:
                    if item["kind"] == "private":
                        invite = await call(functions.messages.CheckChatInviteRequest(item["value"]))
                        if not isinstance(invite, types.ChatInviteAlready):
                            raise InputError("Не удалось определить приватный чат")
                        item = pack(invite.chat, True, item["source"], item["kind"], item["value"])
                await wait_or_stop(stop, delay)
            resolved.append(item)
        except (errors.FloodWaitError, errors.PeerFloodError):
            raise
        except Exception as exc:
            issues.append({**original, "reason": error_text(exc)})
            await wait_or_stop(stop, delay)
    unique = {}
    for item in resolved:
        unique.setdefault(str(item["id"]), item)
    return list(unique.values())


def report_csv(store, job_id):
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["Задача", "Цикл", "Аккаунт", "ID чата", "Название чата", "Ссылка", "Статус", "Причина"])
    def cell(value):
        text = str(value or "")
        return "'" + text if text.startswith(("=", "+", "-", "@", "\t", "\r")) else text
    for row in store.deliveries(job_id):
        writer.writerow([job_id, row["cycle"] + 1, row["account"], row["chat"],
                         cell(row["title"]), cell(row["source"]), row["status"], cell(row["reason"])])
    return output.getvalue().encode("utf-8-sig")


class Runner:
    def __init__(self, store, notify, reporter=None):
        self.store = store
        self.notify = notify
        self.reporter = reporter
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

    async def rpc(self, operation):
        while True:
            if self.stop.is_set():
                raise asyncio.CancelledError()
            try:
                return await operation()
            except errors.FloodWaitError as exc:
                seconds = max(1, int(exc.seconds))
                text = f"Telegram требует паузу {seconds} сек. Жду на текущем аккаунте. Остановка: /stop"
                self.store.state(self.job_id, "running", text)
                await self.safe_notify(text)
                await wait_or_stop(self.stop, seconds)

    async def send(self, client, item, snapshot):
        media = snapshot.get("media")
        if not media:
            return await client.send_message(input_peer(item), snapshot["message"],
                                             parse_mode=None, link_preview=False)
        with io.BytesIO(self.store.read_media(media["id"])) as file:
            file.name = media["name"]
            kwargs = {}
            if media["kind"] == "video":
                kwargs["mime_type"] = media.get("mime_type") or "video/mp4"
                kwargs["supports_streaming"] = kwargs["mime_type"] == "video/mp4"
                kwargs["attributes"] = [types.DocumentAttributeVideo(
                    duration=media.get("duration") or 0,
                    w=media.get("width") or 0, h=media.get("height") or 0,
                    supports_streaming=kwargs["supports_streaming"])]
            return await client.send_file(input_peer(item), file, caption=snapshot["message"],
                                          parse_mode=None, force_document=False, **kwargs)

    def summary(self, job_id):
        rows = self.store.deliveries(job_id)
        sent = sum(r["status"] == "sent" for r in rows)
        failed = sum(r["status"] == "failed" for r in rows)
        pending = sum(r["status"] == "pending" for r in rows)
        return f"Отправлено: {sent}. Ошибок/пропусков: {failed}. Требует проверки: {pending}."

    async def run(self, job_id):
        try:
            job = self.store.job(job_id)
            snapshot = job["snapshot"]
            cycle = job["cycle"]
            # Previously saved unlimited jobs retain their original semantics.
            limit = snapshot.get("cycles", None if snapshot.get("repeat") else 1)
            while limit is None or cycle < limit:
                message_attempted = False
                for account_plan in snapshot["accounts"]:
                    if self.stop.is_set():
                        raise asyncio.CancelledError()
                    account_id = account_plan["account_id"]
                    for issue in account_plan.get("issues", []):
                        if not self.store.recorded(job_id, cycle, account_id, issue["id"]):
                            self.store.failed(job_id, cycle, account_id, issue, issue["reason"])
                    client = None
                    try:
                        payload = self.store.account(account_id)
                        client = make_client(StringSession(payload["session"]), payload["options"])
                        await client.connect()
                        if not await self.rpc(client.is_user_authorized):
                            raise InputError("Сессия аккаунта отозвана. Переподключите аккаунт")
                        issues = []
                        items = await ensure_joined(client, account_plan["items"], self.stop,
                                                    snapshot["delay"], issues, self.rpc)
                        for issue in issues:
                            if not self.store.recorded(job_id, cycle, account_id, issue["id"]):
                                self.store.failed(job_id, cycle, account_id, issue, issue["reason"])
                        for item in items:
                            if self.stop.is_set():
                                raise asyncio.CancelledError()
                            if self.store.recorded(job_id, cycle, account_id, item["id"]):
                                continue
                            if message_attempted:
                                await wait_or_stop(self.stop, snapshot["delay"])
                            if not self.store.claim(job_id, cycle, account_id, item["id"],
                                                    item.get("title", ""), item.get("source", "")):
                                continue
                            message_attempted = True
                            try:
                                await self.rpc(lambda: self.send(client, item, snapshot))
                            except errors.PeerFloodError as exc:
                                self.store.failed(job_id, cycle, account_id, item, error_text(exc))
                                raise  # Account-wide restriction: never rotate accounts to evade it.
                            except Exception as exc:
                                uncertain = (isinstance(exc, (OSError, errors.ServerError))
                                             or not isinstance(exc, (errors.RPCError, InputError)))
                                self.store.failed(job_id, cycle, account_id, item, error_text(exc), uncertain)
                                continue
                            self.store.sent(job_id, cycle, account_id, item["id"])
                            self.store.state(job_id, "running", f"Цикл {cycle + 1}. " + self.summary(job_id))
                    except errors.PeerFloodError:
                        raise
                    except Exception as exc:
                        for item in account_plan["items"]:
                            if not self.store.recorded(job_id, cycle, account_id, item["id"]):
                                self.store.failed(job_id, cycle, account_id, item, error_text(exc))
                    finally:
                        if client:
                            try:
                                await client.disconnect()
                            except Exception:
                                pass
                cycle += 1
                self.store.progress(job_id, cycle)
                if limit is not None and cycle >= limit:
                    break
                self.store.state(job_id, "running", f"Завершено циклов: {cycle}. "
                                 f"Следующий через {snapshot['repeat']} сек. " + self.summary(job_id))
                await wait_or_stop(self.stop, snapshot["repeat"])
            self.store.state(job_id, "done", f"Завершено циклов: {cycle}. " + self.summary(job_id))
        except asyncio.CancelledError:
            self.store.state(job_id, "stopped", "Остановлено пользователем. " + self.summary(job_id))
        except Exception as exc:
            self.store.state(job_id, "error", error_text(exc))
        finally:
            await self.finish_report(job_id)

    async def finish_report(self, job_id):
        job = self.store.job(job_id)
        state = {"done": "завершена", "stopped": "остановлена", "error": "прервана"}.get(job["state"], job["state"])
        lines = [f"Задача {job_id} {state}.", f"Завершено циклов: {job['cycle']}.", self.summary(job_id)]
        if job["state"] == "error":
            lines.append(job["detail"])
        failed = [r for r in self.store.deliveries(job_id) if r["status"] != "sent"]
        if failed:
            lines.append("Не отправлено / требуется проверка:")
            lines.extend(f"• {r['title'][:80] or r['chat']} — {r['reason'][:180] or 'Нет подтверждения отправки'}"
                         for r in failed[:10])
            if len(failed) > 10:
                lines.append(f"Ещё записей: {len(failed) - 10}. Полный список — в отчёте CSV.")
        await self.safe_notify("\n".join(lines)[:3900])
        if self.reporter:
            try:
                await self.reporter(job_id)
            except Exception:
                pass

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

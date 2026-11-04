import asyncio
from contextlib import closing
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import zipfile

from cryptography.fernet import Fernet
from telethon import errors, functions, types, utils
from telethon.crypto import AuthKey
from telethon.sessions import StringSession

from accounts import InputError, from_key, from_sqlite, unpack_tdata
from delivery import Runner, ensure_joined, pack, parse_targets, prepare
from main import allowed, build_app, Panel
from storage import Store


def session_string():
    session = StringSession()
    session.set_dc(2, "127.0.0.1", 443)
    session.auth_key = AuthKey(bytes(range(256)))
    return session.save()


def channel(chat_id=123):
    return types.Channel(chat_id, "Test chat", types.ChatPhotoEmpty(), None,
                         megagroup=True, access_hash=789)


class FakeClient:
    def __init__(self, joined=None, invite=None, fail=False):
        self.joined = list(joined or [])
        self.invite = invite
        self.calls = []
        self.sent = []
        self.fail = fail
        self.connect = AsyncMock()
        self.disconnect = AsyncMock()
        self.is_user_authorized = AsyncMock(return_value=True)

    async def iter_dialogs(self):
        for entity in self.joined:
            yield SimpleNamespace(entity=entity)

    async def get_entity(self, name):
        return channel()

    async def __call__(self, request):
        self.calls.append(request)
        if isinstance(request, (functions.chatlists.CheckChatlistInviteRequest,
                                functions.messages.CheckChatInviteRequest)):
            return self.invite
        return SimpleNamespace(chats=[channel()])

    async def send_message(self, entity, text, **kwargs):
        if self.fail:
            raise errors.FloodWaitError(request=None, capture=42)
        self.sent.append((entity, text))


class ImportTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_key_validation_and_dc(self):
        opts = {"api_id": 1, "api_hash": "a" * 32}
        with self.assertRaises(InputError):
            await from_key("abc...def", 1, opts)
        with patch("accounts.dc_address", AsyncMock(return_value=("127.0.0.1", 443))):
            client = await from_key(bytes(range(256)), 1, opts)
            self.assertEqual(len(client.session.auth_key.key), 256)
            self.assertEqual(client.session.dc_id, 1)

    async def test_pyrogram_and_telethon_sqlite_import(self):
        opts = {"api_id": 1, "api_hash": "a" * 32}
        for kind in ("telethon", "pyrogram"):
            with tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "source.db"
                with closing(sqlite3.connect(path)) as db:
                    db.execute("CREATE TABLE sessions(dc_id INTEGER,auth_key BLOB,test_mode INTEGER,is_bot INTEGER)")
                    db.execute("INSERT INTO sessions VALUES(?,?,0,0)", (2, bytes(range(256))))
                    db.commit()
                with patch("accounts.dc_address", AsyncMock(return_value=("127.0.0.1", 443))):
                    client = await from_sqlite(path.read_bytes(), kind, opts)
                    self.assertEqual(client.session.auth_key.key, bytes(range(256)))

    def test_zip_slip_is_rejected(self):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("../outside", b"test")
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(InputError):
                unpack_tdata(output.getvalue(), folder)


class PlanTests(unittest.IsolatedAsyncioTestCase):
    async def test_folder_plan_is_read_only_and_deduplicated(self):
        chat = channel()
        invite = types.chatlists.ChatlistInvite(types.TextWithEntities("Folder", []),
                                               [types.PeerChannel(chat.id)], [chat], [])
        client = FakeClient(invite=invite)
        plan = await prepare(client, ["https://t.me/addlist/example", "@example"])
        self.assertEqual(len(plan), 1)
        self.assertFalse(plan[0]["joined"])
        self.assertTrue(all(isinstance(r, functions.chatlists.CheckChatlistInviteRequest)
                            for r in client.calls))
        self.assertEqual(client.sent, [])

    async def test_existing_folder_does_not_join_already_member(self):
        chat = channel()
        client = FakeClient(joined=[chat])
        plan = [pack(chat, False, "link", "folder", "slug")]
        result = await ensure_joined(client, plan, asyncio.Event(), 0)
        self.assertEqual(client.calls, [])
        self.assertEqual(len(result), 1)

    async def test_missing_folder_chat_is_joined(self):
        client = FakeClient()
        plan = [pack(channel(), False, "link", "folder", "slug")]
        await ensure_joined(client, plan, asyncio.Event(), 0)
        self.assertIsInstance(client.calls[0], functions.chatlists.JoinChatlistInviteRequest)
        self.assertEqual(len(client.calls[0].peers), 1)

    async def test_private_invite_is_resolved_after_join(self):
        client = FakeClient()
        plan = [{"id": "invite:hash", "title": "Private", "joined": False,
                 "kind": "private", "value": "hash", "source": "link"}]
        result = await ensure_joined(client, plan, asyncio.Event(), 0)
        self.assertEqual(result[0]["id"], utils.get_peer_id(channel()))
        self.assertIsInstance(client.calls[0], functions.messages.ImportChatInviteRequest)

    def test_target_validation(self):
        self.assertEqual(len(parse_targets("@example\nhttps://t.me/example\nhttps://t.me/addlist/X")), 2)
        for value in ("https://evil.test/chat", "https://t.me/example/123", "https://t.me/c/123/4"):
            with self.assertRaises(InputError): parse_targets(value)


class StorageAndRunnerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.key = Fernet.generate_key()
        self.store = Store(self.temp.name, self.key)
        self.addCleanup(self.store.close)
        self.store.save_account(10, "Account", {"session": session_string(),
            "options": {"api_id": 1, "api_hash": "a" * 32}})
        self.item = pack(channel(), True, "@example", "public", "example")
        self.store.new_job("test", {"message": "hello", "delay": 0, "repeat": 0,
            "accounts": [{"account_id": 10, "items": [self.item]}]})

    def test_session_is_encrypted(self):
        encrypted = self.store.db.execute("SELECT secret FROM accounts").fetchone()[0]
        self.assertNotIn(session_string().encode(), encrypted)
        self.assertEqual(self.store.account(10)["session"], session_string())

    async def test_run_sends_once_and_finished_job_cannot_restart(self):
        client = FakeClient(joined=[channel()])
        runner = Runner(self.store, AsyncMock())
        with patch("delivery.make_client", return_value=client):
            runner.launch("test")
            await runner.task
        self.assertEqual(len(client.sent), 1)
        self.assertEqual(self.store.job("test")["state"], "done")
        with self.assertRaises(InputError): runner.launch("test")

    async def test_flood_wait_stops_all_accounts_and_pending_is_preserved(self):
        client = FakeClient(joined=[channel()], fail=True)
        runner = Runner(self.store, AsyncMock())
        with patch("delivery.make_client", return_value=client):
            runner.launch("test")
            await runner.task
        self.assertEqual(self.store.job("test")["state"], "error")
        self.assertIn("42", self.store.job("test")["detail"])
        self.assertEqual(self.store.deliveries("test")[0]["status"], "pending")
        client.fail = False
        with patch("delivery.make_client", return_value=client):
            runner.launch("test")
            await runner.task
        self.assertEqual(client.sent, [])

    async def test_stop_prevents_send(self):
        runner = Runner(self.store, AsyncMock())
        runner.launch("test")
        runner.stop.set()
        await runner.task
        self.assertEqual(self.store.job("test")["state"], "stopped")
        self.assertEqual(self.store.deliveries("test"), [])

    def test_owner_gate(self):
        update = SimpleNamespace(effective_user=SimpleNamespace(id=10),
                                 effective_chat=SimpleNamespace(type="private"))
        self.assertTrue(allowed(update, 10))
        self.assertFalse(allowed(update, 11))
        update.effective_chat.type = "group"
        self.assertFalse(allowed(update, 10))

    async def test_unauthorized_upload_is_never_downloaded(self):
        panel = Panel(self.store, 10, 1, "a" * 32)
        document = SimpleNamespace(get_file=AsyncMock())
        update = SimpleNamespace(effective_user=SimpleNamespace(id=11),
                                 effective_chat=SimpleNamespace(type="private"),
                                 message=SimpleNamespace(document=document))
        await panel.content(update, None)
        document.get_file.assert_not_called()

    def test_application_builds_without_network(self):
        with patch.dict(os.environ, {"BOT_TOKEN": "123456789:" + "x" * 35,
                        "OWNER_ID": "10", "TELEGRAM_API_ID": "1",
                        "TELEGRAM_API_HASH": "a" * 32,
                        "ENCRYPTION_KEY": self.key.decode(), "DATA_DIR": self.temp.name}):
            app = build_app()
            self.assertGreater(len(app.handlers[0]), 5)
            app.post_shutdown.__self__.store.close()


if __name__ == "__main__":
    unittest.main()

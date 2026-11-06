"""Persistent state. Session strings and API credentials are encrypted at rest."""
import json
import secrets
import sqlite3
import time
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


DEFAULTS = {"message": "", "targets": [], "delay": 30, "repeat": 0, "cycles": 1}


class Store:
    def __init__(self, root, key):
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        self.cipher = Fernet(key.encode() if isinstance(key, str) else key)
        self.db = sqlite3.connect(root / "bot.sqlite3")
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY, value BLOB);
            CREATE TABLE IF NOT EXISTS accounts
                (id INTEGER PRIMARY KEY, label TEXT, selected INTEGER, secret BLOB);
            CREATE TABLE IF NOT EXISTS jobs
                (id TEXT PRIMARY KEY, state TEXT, snapshot BLOB, detail TEXT, created REAL);
            CREATE TABLE IF NOT EXISTS deliveries
                (job TEXT, cycle INTEGER, account INTEGER, chat TEXT, status TEXT,
                 PRIMARY KEY(job,cycle,account,chat));
            CREATE TABLE IF NOT EXISTS media (id TEXT PRIMARY KEY, data BLOB);
        """)
        for table, columns in {
            "deliveries": {"title": "TEXT DEFAULT ''", "source": "TEXT DEFAULT ''", "reason": "TEXT DEFAULT ''"},
            "jobs": {"cycle": "INTEGER DEFAULT 0"},
        }.items():
            present = {row[1] for row in self.db.execute(f"PRAGMA table_info({table})")}
            for column, definition in columns.items():
                if column not in present:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        self.db.execute("UPDATE jobs SET state='interrupted',detail='Процесс перезапущен' "
                        "WHERE state='running'")
        self.db.commit()
        # Detect a wrong encryption key before starting to overwrite any settings.
        for table, column in (("settings", "value"), ("accounts", "secret"), ("jobs", "snapshot")):
            row = self.db.execute(f"SELECT {column} FROM {table} LIMIT 1").fetchone()
            if row:
                try:
                    self.decrypt(row[0])
                except InvalidToken:
                    self.db.close()
                    raise ValueError("ENCRYPTION_KEY не подходит к существующему хранилищу") from None

    def encrypt(self, value):
        return self.cipher.encrypt(json.dumps(value, ensure_ascii=False).encode())

    def decrypt(self, value):
        return json.loads(self.cipher.decrypt(value))

    def settings(self):
        row = self.db.execute("SELECT value FROM settings WHERE id=1").fetchone()
        return {**DEFAULTS, **(self.decrypt(row[0]) if row else {})}

    def set_settings(self, value):
        self.db.execute("INSERT OR REPLACE INTO settings VALUES(1,?)", (self.encrypt(value),))
        self.db.commit()

    def save_media(self, data):
        media_id = secrets.token_hex(16)
        self.db.execute("INSERT INTO media VALUES(?,?)", (media_id, self.cipher.encrypt(data)))
        self.db.commit()
        return media_id

    def read_media(self, media_id):
        row = self.db.execute("SELECT data FROM media WHERE id=?", (media_id,)).fetchone()
        if not row:
            raise ValueError("Вложение не найдено. Задайте сообщение заново и создайте новый план")
        return self.cipher.decrypt(row[0])

    def accounts(self):
        return [dict(r) for r in self.db.execute("SELECT id,label,selected FROM accounts ORDER BY id")]

    def account(self, account_id):
        row = self.db.execute("SELECT secret FROM accounts WHERE id=?", (account_id,)).fetchone()
        if not row:
            raise ValueError("Аккаунт удалён")
        return self.decrypt(row[0])

    def save_account(self, user_id, label, payload):
        old = self.db.execute("SELECT selected FROM accounts WHERE id=?", (user_id,)).fetchone()
        self.db.execute("INSERT OR REPLACE INTO accounts VALUES(?,?,?,?)",
                        (user_id, label, old[0] if old else 1, self.encrypt(payload)))
        self.db.commit()

    def toggle(self, user_id):
        self.db.execute("UPDATE accounts SET selected=1-selected WHERE id=?", (user_id,))
        self.db.commit()

    def delete_account(self, user_id):
        self.db.execute("DELETE FROM accounts WHERE id=?", (user_id,))
        self.db.commit()

    def new_job(self, job_id, snapshot):
        self.db.execute("INSERT INTO jobs(id,state,snapshot,detail,created) VALUES(?,'draft',?,'',?)",
                        (job_id, self.encrypt(snapshot), time.time()))
        self.db.commit()

    def job(self, job_id):
        row = self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise ValueError("Задача не найдена")
        result = dict(row)
        result["snapshot"] = self.decrypt(result["snapshot"])
        return result

    def jobs(self):
        return [dict(r) for r in self.db.execute(
            "SELECT id,state,detail,created FROM jobs ORDER BY created DESC LIMIT 10")]

    def state(self, job_id, state, detail=""):
        self.db.execute("UPDATE jobs SET state=?,detail=? WHERE id=?", (state, detail, job_id))
        self.db.commit()

    def progress(self, job_id, cycle):
        self.db.execute("UPDATE jobs SET cycle=? WHERE id=?", (cycle, job_id))
        self.db.commit()

    def recorded(self, job_id, cycle, account_id, chat):
        return self.db.execute("SELECT 1 FROM deliveries WHERE job=? AND cycle=? AND account=? AND chat=?",
                               (job_id, cycle, account_id, str(chat))).fetchone() is not None

    def claim(self, job_id, cycle, account_id, chat, title="", source=""):
        cur = self.db.execute("INSERT OR IGNORE INTO deliveries "
                              "(job,cycle,account,chat,status,title,source) VALUES(?,?,?,?,'pending',?,?)",
                              (job_id, cycle, account_id, str(chat), title, source))
        self.db.commit()
        return cur.rowcount == 1

    def sent(self, job_id, cycle, account_id, chat):
        self.db.execute("UPDATE deliveries SET status='sent',reason='' "
                        "WHERE job=? AND cycle=? AND account=? AND chat=?",
                        (job_id, cycle, account_id, str(chat)))
        self.db.commit()

    def failed(self, job_id, cycle, account_id, item, reason, uncertain=False):
        values = (job_id, cycle, account_id, str(item["id"]),
                  "pending" if uncertain else "failed", item.get("title", ""),
                  item.get("source", ""), reason)
        self.db.execute("INSERT INTO deliveries(job,cycle,account,chat,status,title,source,reason) "
                        "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(job,cycle,account,chat) DO UPDATE SET "
                        "status=excluded.status,reason=excluded.reason,title=excluded.title,source=excluded.source "
                        "WHERE deliveries.status != 'sent'", values)
        self.db.commit()

    def deliveries(self, job_id):
        return [dict(r) for r in self.db.execute("SELECT * FROM deliveries WHERE job=?", (job_id,))]

    def close(self):
        self.db.close()

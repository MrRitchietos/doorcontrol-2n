import os
import sqlite3
import threading

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS roles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    rank INTEGER NOT NULL DEFAULT 10,
    is_admin INTEGER NOT NULL DEFAULT 0,
    manage_users INTEGER NOT NULL DEFAULT 0,
    manage_doors INTEGER NOT NULL DEFAULT 0,
    manage_settings INTEGER NOT NULL DEFAULT 0,
    view_log INTEGER NOT NULL DEFAULT 0,
    all_doors INTEGER NOT NULL DEFAULT 0,
    days TEXT NOT NULL DEFAULT '1234567',
    time_from TEXT NOT NULL DEFAULT '',
    time_to TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    name TEXT NOT NULL DEFAULT '',
    password_hash TEXT NOT NULL,
    role_id INTEGER NOT NULL REFERENCES roles(id),
    active INTEGER NOT NULL DEFAULT 1,
    token INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_login TEXT
);

CREATE TABLE IF NOT EXISTS devices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    host TEXT NOT NULL,
    scheme TEXT NOT NULL DEFAULT 'http',
    username TEXT NOT NULL DEFAULT '',
    password TEXT NOT NULL DEFAULT '',
    verify_tls INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS buttons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    section TEXT NOT NULL DEFAULT '',
    icon TEXT NOT NULL DEFAULT 'door',
    color TEXT NOT NULL DEFAULT '',
    hold_to_open INTEGER NOT NULL DEFAULT 0,
    sort INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    button_id INTEGER NOT NULL REFERENCES buttons(id) ON DELETE CASCADE,
    device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    kind TEXT NOT NULL DEFAULT 'switch',
    value TEXT NOT NULL DEFAULT '1',
    action TEXT NOT NULL DEFAULT 'trigger'
);

CREATE TABLE IF NOT EXISTS role_buttons (
    role_id INTEGER NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
    button_id INTEGER NOT NULL REFERENCES buttons(id) ON DELETE CASCADE,
    PRIMARY KEY (role_id, button_id)
);

CREATE TABLE IF NOT EXISTS log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL DEFAULT (datetime('now')),
    user_id INTEGER,
    username TEXT NOT NULL DEFAULT '',
    event TEXT NOT NULL,
    target TEXT NOT NULL DEFAULT '',
    ok INTEGER NOT NULL DEFAULT 1,
    detail TEXT NOT NULL DEFAULT '',
    ip TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS log_ts ON log(ts);
"""

DEFAULT_SETTINGS = {
    "app_name": "Door Control",
    "subtitle": "",
    "accent": "#2563eb",
    "language": "en",
    "timezone": "Europe/Brussels",
    "session_days": "30",
    "log_days": "180",
    "logo_version": "0",
}


class Database:
    def __init__(self, path):
        self.path = path
        self.local = threading.local()
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with self.conn() as c:
            c.executescript(SCHEMA)
            for k, v in DEFAULT_SETTINGS.items():
                c.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v))
            if not c.execute("SELECT 1 FROM roles").fetchone():
                c.execute(
                    "INSERT INTO roles(name, rank, is_admin, manage_users, manage_doors, manage_settings,"
                    " view_log, all_doors) VALUES ('Admin', 1, 1, 1, 1, 1, 1, 1)"
                )
                c.execute("INSERT INTO roles(name, rank, all_doors) VALUES ('User', 10, 1)")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def conn(self):
        c = getattr(self.local, "c", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=10)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA foreign_keys = ON")
            c.execute("PRAGMA journal_mode = WAL")
            self.local.c = c
        return c

    def all(self, sql, args=()):
        return [dict(r) for r in self.conn().execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        r = self.conn().execute(sql, args).fetchone()
        return dict(r) if r else None

    def run(self, sql, args=()):
        c = self.conn()
        with c:
            cur = c.execute(sql, args)
        return cur.lastrowid

    def settings(self):
        return {r["key"]: r["value"] for r in self.all("SELECT key, value FROM settings")}

    def set_setting(self, key, value):
        self.run("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)", (key, str(value)))

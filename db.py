"""
Database access for the app.

* On this PC: the SQLite file vendors.db (as before).
* Online (Vercel): a Postgres database, used automatically when the
  DATABASE_URL (or POSTGRES_URL) environment variable is set - e.g. by
  connecting a Neon database in Vercel -> Storage.

The rest of the app writes SQLite-style SQL ("?" placeholders); this module
translates it for Postgres, so both work with the same code.
"""

import os
import re
import sqlite3
import threading

POSTGRES_URL = (os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL") or "").strip()
IS_POSTGRES = POSTGRES_URL.startswith(("postgres://", "postgresql://"))

DB_PATH = None          # set by app.py for SQLite


class Row(tuple):
    """A row that works like sqlite3.Row: row[0], row["name"], dict(row)."""

    def __new__(cls, values, names):
        row = tuple.__new__(cls, values)
        row._names = names
        return row

    def __getitem__(self, key):
        if isinstance(key, str):
            return tuple.__getitem__(self, self._names.index(key))
        return tuple.__getitem__(self, key)

    def keys(self):
        return list(self._names)


def _row_factory(cursor):
    names = [d.name for d in (cursor.description or [])]
    return lambda values: Row(values, names)


_local = threading.local()


def _pg_connection():
    import psycopg
    conn = getattr(_local, "conn", None)
    if conn is None or conn.closed or conn.broken:
        conn = psycopg.connect(POSTGRES_URL, row_factory=_row_factory, autocommit=False,
                               connect_timeout=15)
        _local.conn = conn
    return conn


def _translate(sql):
    """SQLite SQL -> Postgres SQL."""
    sql = sql.replace("%", "%%").replace("?", "%s")
    sql = re.sub(r"\bLIKE\b", "ILIKE", sql)                       # case-insensitive search
    sql = re.sub(r"\bINTEGER PRIMARY KEY AUTOINCREMENT\b", "SERIAL PRIMARY KEY", sql)
    sql = re.sub(r"\bBLOB\b", "BYTEA", sql)
    return sql


class Cursor:
    def __init__(self, cursor, lastrowid=None):
        self._cursor = cursor
        self.lastrowid = lastrowid

    def fetchone(self):
        try:
            return self._cursor.fetchone()
        except Exception:
            return None

    def fetchall(self):
        try:
            return self._cursor.fetchall()
        except Exception:
            return []

    def __iter__(self):
        return iter(self.fetchall())


class Connection:
    """Small wrapper with the parts of the sqlite3 API the app uses."""

    def __init__(self):
        if IS_POSTGRES:
            self._conn = _pg_connection()
        else:
            self._conn = sqlite3.connect(DB_PATH)
            self._conn.row_factory = sqlite3.Row

    # sqlite3 code sets this; rows are always dict-like here
    row_factory = None

    def execute(self, sql, params=()):
        if not IS_POSTGRES:
            cursor = self._conn.execute(sql, params)
            return Cursor(cursor, cursor.lastrowid)

        sql = _translate(sql)
        returning = None
        if re.match(r"\s*INSERT\s+INTO\s+vendors\b", sql, re.I) and "RETURNING" not in sql.upper():
            sql = sql.rstrip().rstrip(";") + " RETURNING id"
            returning = True
        cursor = self._conn.cursor()
        cursor.execute(sql, params)
        lastrowid = None
        if returning:
            lastrowid = cursor.fetchone()[0]
        return Cursor(cursor, lastrowid)

    def columns(self, table):
        """Column names of a table."""
        if IS_POSTGRES:
            rows = self.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = ?", (table,))
            return {r[0] for r in rows}
        return {r[1] for r in self._conn.execute(f"PRAGMA table_info({table})")}

    def commit(self):
        self._conn.commit()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type:
            self._conn.rollback()
        else:
            self._conn.commit()
        if not IS_POSTGRES:
            self._conn.close()
        return False


def connect():
    return Connection()


def backup_bytes():
    """A copy of the database for "Download backup" (SQLite only)."""
    if IS_POSTGRES:
        return None
    with sqlite3.connect(DB_PATH) as conn:
        return conn.serialize()

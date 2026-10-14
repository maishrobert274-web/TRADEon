"""Small SQLite/PostgreSQL compatibility layer for TRADEON.

When DATABASE_URL is set, application data is stored in PostgreSQL.
Without it, local SQLite remains available for development.
"""
import os
import re
import sqlite3

try:
    import psycopg2
    from psycopg2.extras import DictCursor
except ImportError:  # SQLite-only local development
    psycopg2 = None
    DictCursor = None

DBIntegrityError = (sqlite3.IntegrityError, psycopg2.IntegrityError) if psycopg2 else sqlite3.IntegrityError


class HybridRow(tuple):
    """Tuple-compatible row that also supports row['column_name']."""
    def __new__(cls, values, names):
        obj = super().__new__(cls, values)
        obj._names = list(names)
        return obj

    def __getitem__(self, key):
        if isinstance(key, str):
            return super().__getitem__(self._names.index(key))
        return super().__getitem__(key)

    def keys(self):
        return list(self._names)


class CursorAdapter:
    def __init__(self, cursor, lastrowid=None, first_row=None):
        self._cursor = cursor
        self.lastrowid = lastrowid
        self._first_row = first_row

    def fetchone(self):
        if self._first_row is not None:
            row, self._first_row = self._first_row, None
            return row
        return self._cursor.fetchone()

    def fetchall(self):
        rows = self._cursor.fetchall()
        if self._first_row is not None:
            rows.insert(0, self._first_row)
            self._first_row = None
        return rows

    def __iter__(self):
        if self._first_row is not None:
            yield self.fetchone()
        yield from self._cursor


class PGConnection:
    def __init__(self, raw):
        self.raw = raw
        self.row_factory = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type:
            self.raw.rollback()
        else:
            self.raw.commit()
        self.raw.close()
        return False

    def close(self):
        self.raw.close()

    def commit(self):
        self.raw.commit()

    def rollback(self):
        self.raw.rollback()

    def _columns(self, table):
        cur = self.raw.cursor()
        cur.execute(
            "SELECT column_name, data_type, is_nullable, column_default "
            "FROM information_schema.columns WHERE table_schema='public' AND table_name=%s "
            "ORDER BY ordinal_position", (table,)
        )
        rows = cur.fetchall()
        cur.close()
        return rows

    def execute(self, sql, params=()):
        pragma = re.match(r'\s*PRAGMA\s+table_info\(["\']?([\w]+)["\']?\)\s*;?$', sql, re.I)
        if pragma:
            columns = self._columns(pragma.group(1))
            names = ["cid", "name", "type", "notnull", "dflt_value", "pk"]
            rows = []
            for i, (name, dtype, nullable, default) in enumerate(columns):
                rows.append(HybridRow((i, name, dtype, int(nullable == "NO"), default, int(name == "id" and "nextval" in str(default))), names))
            return RowsCursor(rows)

        if "FROM sqlite_master" in sql:
            cur = self.raw.cursor(cursor_factory=DictCursor)
            cur.execute("SELECT table_name AS name FROM information_schema.tables "
                        "WHERE table_schema='public' AND table_type='BASE TABLE' ORDER BY table_name")
            return CursorAdapter(cur)

        q = sql
        q = re.sub(r'INSERT\s+OR\s+IGNORE\s+INTO', 'INSERT INTO', q, flags=re.I)
        replace_settings = bool(re.search(r'INSERT\s+OR\s+REPLACE\s+INTO\s+app_settings', q, re.I))
        q = re.sub(r'INSERT\s+OR\s+REPLACE\s+INTO', 'INSERT INTO', q, flags=re.I)
        q = re.sub(r'INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT', 'BIGSERIAL PRIMARY KEY', q, flags=re.I)
        q = re.sub(r'DEFAULT\s+"(PENDING|OPEN|SUCCESS|FAILED)"', r"DEFAULT '\1'", q, flags=re.I)
        q = re.sub(r"datetime\('now',\s*'\+'\s*\|\|\s*\?\s*\|\|\s*' seconds'\)", "(CURRENT_TIMESTAMP + (%s * INTERVAL '1 second'))", q, flags=re.I)
        q = re.sub(r'datetime\(\?\)', '(CAST(%s AS timestamp))', q, flags=re.I)
        q = q.replace("?", "%s")

        add_returning = bool(re.match(r'\s*INSERT\s+INTO\s+(users|trades)\s*\(', q, re.I)) and "RETURNING" not in q.upper()
        if replace_settings:
            q = q.rstrip().rstrip(";") + " ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value"
        elif re.search(r'INSERT\s+INTO\s+(users|wallets|demo_wallets|app_settings)\b', q, re.I) and "ON CONFLICT" not in q.upper():
            # INSERT OR IGNORE statements are identified by their original SQL.
            if re.search(r'INSERT\s+OR\s+IGNORE', sql, re.I):
                q = q.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
        if add_returning:
            q = q.rstrip().rstrip(";") + " RETURNING id"

        cur = self.raw.cursor(cursor_factory=DictCursor)
        cur.execute(q, params or ())
        lastrowid = None
        first_row = None
        if add_returning:
            first_row = cur.fetchone()
            if first_row is not None:
                lastrowid = first_row["id"]
        return CursorAdapter(cur, lastrowid=lastrowid, first_row=first_row)

    def executemany(self, sql, seq):
        # The app only uses executemany for simple settings inserts.
        for params in seq:
            self.execute(sql, params)
        return None


class RowsCursor:
    def __init__(self, rows):
        self.rows = rows
    def fetchall(self):
        rows, self.rows = self.rows, []
        return rows
    def fetchone(self):
        return self.rows.pop(0) if self.rows else None


def db_connect():
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        return sqlite3.connect("tradeon.db")
    if not psycopg2:
        raise RuntimeError("DATABASE_URL is set but psycopg2 is not installed.")
    url = url.replace("postgres://", "postgresql://", 1) if url.startswith("postgres://") else url
    raw = psycopg2.connect(url, connect_timeout=10, sslmode="require")
    return PGConnection(raw)

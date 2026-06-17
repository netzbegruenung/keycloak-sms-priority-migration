"""
Test infrastructure using an in-memory DuckDB database instead of a real
PostgreSQL instance. A thin adapter translates psycopg2-style %(name)s
parameters and RealDictCursor semantics so migrate.py's functions are called
unchanged. Requires only: duckdb (no Docker, no PostgreSQL installation).
"""

import re
import uuid

import duckdb
import pytest

_PARAM_RE = re.compile(r"%\((\w+)\)s")

_SCHEMA = """
CREATE TABLE realm (
    id   VARCHAR PRIMARY KEY,
    name VARCHAR UNIQUE NOT NULL
);
CREATE TABLE user_entity (
    id       VARCHAR PRIMARY KEY,
    realm_id VARCHAR NOT NULL,
    username VARCHAR NOT NULL
);
CREATE TABLE credential (
    id       VARCHAR PRIMARY KEY,
    user_id  VARCHAR NOT NULL,
    type     VARCHAR NOT NULL,
    priority INT NOT NULL
);
"""


class _Cursor:
    """psycopg2 RealDictCursor-compatible cursor backed by DuckDB."""

    def __init__(self, duckdb_conn):
        self._conn = duckdb_conn
        self.rowcount = -1
        self._rows = []
        self._cols = []

    def execute(self, sql, params=None):
        # Translate %(name)s → $name for DuckDB named parameters
        sql = _PARAM_RE.sub(r"$\1", sql)
        rel = self._conn.execute(sql, params or [])
        desc = rel.description or []
        # DML (INSERT/UPDATE/DELETE) and DDL return a single 'Count' column
        if len(desc) == 1 and desc[0][0] == "Count":
            rows = rel.fetchall()
            self.rowcount = rows[0][0] if rows else 0
            self._rows = []
            self._cols = []
        else:
            self._cols = [d[0].lower() for d in desc]
            self._rows = rel.fetchall()
            self.rowcount = -1

    def fetchall(self):
        return [dict(zip(self._cols, row)) for row in self._rows]

    def fetchone(self):
        rows = self.fetchall()
        return rows[0] if rows else None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class _Connection:
    """
    psycopg2-compatible connection backed by an in-memory DuckDB database.

    commit() is a no-op (DuckDB auto-commits by default).
    rollback() is a no-op; test isolation is achieved by creating a fresh
    in-memory connection per test via the db_conn fixture.
    """

    def __init__(self):
        self._db = duckdb.connect()

    def cursor(self):
        return _Cursor(self._db)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        self._db.close()


@pytest.fixture
def db_conn():
    """Fresh in-memory database per test, pre-loaded with the minimal schema."""
    conn = _Connection()
    with conn.cursor() as cur:
        cur.execute(_SCHEMA)
    yield conn
    conn.close()


def seed(conn, realm_name, users):
    """
    Insert test data into REALM, USER_ENTITY, and CREDENTIAL.

    users — list of dicts:
        {"username": "alice", "credentials": [{"type": "mobile-number", "priority": 10}, ...]}

    Returns {"realm_id": str, "users": {username: user_id, ...}}.
    """
    realm_id = str(uuid.uuid4())
    result = {"realm_id": realm_id, "users": {}}
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO realm (id, name) VALUES ($id, $name)",
            {"id": realm_id, "name": realm_name},
        )
        for user in users:
            user_id = str(uuid.uuid4())
            result["users"][user["username"]] = user_id
            cur.execute(
                "INSERT INTO user_entity (id, realm_id, username) VALUES ($id, $realm_id, $username)",
                {"id": user_id, "realm_id": realm_id, "username": user["username"]},
            )
            for cred in user["credentials"]:
                cur.execute(
                    "INSERT INTO credential (id, user_id, type, priority) VALUES ($id, $uid, $type, $prio)",
                    {"id": str(uuid.uuid4()), "uid": user_id, "type": cred["type"], "prio": cred["priority"]},
                )
    return result


def sms_priority(conn, user_id):
    """Return the current PRIORITY of the mobile-number credential for user_id, or None."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT priority FROM credential WHERE user_id = $uid AND type = 'mobile-number'",
            {"uid": user_id},
        )
        row = cur.fetchone()
    return row["priority"] if row else None

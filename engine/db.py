"""Postgres (Supabase) access. Everything lives in the `signals` schema."""
from __future__ import annotations

import json
import logging
import os
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .item import Item

log = logging.getLogger("db")

POOLER_HOSTS = [
    "aws-0-us-east-1.pooler.supabase.com",
    "aws-1-us-east-1.pooler.supabase.com",
]

_conn: psycopg.Connection | None = None


def _candidate_dsns() -> list[str]:
    if os.environ.get("DATABASE_URL"):
        return [os.environ["DATABASE_URL"]]
    ref = os.environ.get("SUPABASE_REF")
    pw = os.environ.get("DB_PASSWORD")
    user = os.environ.get("DB_USER", "signal_engine")
    if not (ref and pw):
        raise RuntimeError("Set DATABASE_URL, or SUPABASE_REF + DB_PASSWORD")
    hosts = [os.environ["DB_HOST"]] if os.environ.get("DB_HOST") else POOLER_HOSTS
    return [
        f"postgresql://{user}.{ref}:{pw}@{h}:5432/postgres?sslmode=require&options=-csearch_path%3Dsignals"
        for h in hosts
    ]


def conn() -> psycopg.Connection:
    global _conn
    if _conn is not None and not _conn.closed:
        return _conn
    errors = []
    for dsn in _candidate_dsns():
        try:
            _conn = psycopg.connect(dsn, autocommit=True, row_factory=dict_row, connect_timeout=20,
                                    prepare_threshold=None)
            _conn.execute("set search_path = signals, public")
            return _conn
        except Exception as e:  # try next pooler host
            errors.append(str(e).split("\n")[0])
    raise RuntimeError("DB connect failed: " + " | ".join(errors))


@contextmanager
def tx():
    c = conn()
    with c.transaction():
        yield c


def q(sql: str, params=None) -> list[dict]:
    with conn().cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall() if cur.description else []


def x(sql: str, params=None) -> int:
    with conn().cursor() as cur:
        cur.execute(sql, params)
        return cur.rowcount


def insert_items(items: list[Item]) -> list[int]:
    """Insert items, skip ones already stored. Returns ids of new rows."""
    new_ids: list[int] = []
    if not items:
        return new_ids
    sql = """
        insert into raw_items (source, source_id, kind, url, title, body, author,
                               company_name, company_domain, published_at, meta)
        values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        on conflict (source, source_id) do nothing
        returning id
    """
    seen = set()
    with tx() as c, c.cursor() as cur:
        for it in items:
            key = (it.source, it.source_id)
            if key in seen:
                continue
            seen.add(key)
            cur.execute(sql, (
                it.source, str(it.source_id)[:300], it.kind, it.url, (it.title or "")[:1000],
                (it.body or "")[:8000], it.author, it.company_name, it.company_domain,
                it.published_at, Jsonb(it.meta or {}),
            ))
            row = cur.fetchone()
            if row:
                new_ids.append(row["id"])
    return new_ids


def log_health(source: str, ok: bool, found: int = 0, new: int = 0, error: str | None = None,
               seconds: float = 0.0):
    try:
        x("insert into source_health (source, ok, items_found, items_new, error, seconds) "
          "values (%s,%s,%s,%s,%s,%s)", (source, ok, found, new, (error or "")[:1000] or None, seconds))
    except Exception as e:
        log.warning("health log failed: %s", e)


def kv_get(key: str, default=None):
    rows = q("select value from kv where key=%s", (key,))
    return rows[0]["value"] if rows else default


def kv_set(key: str, value):
    x("insert into kv (key, value, updated_at) values (%s,%s,now()) "
      "on conflict (key) do update set value=excluded.value, updated_at=now()", (key, Jsonb(value)))


def dumps(v) -> str:
    return json.dumps(v, default=str)

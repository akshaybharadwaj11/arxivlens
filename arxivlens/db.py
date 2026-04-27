"""Postgres connection pool used by all services."""
from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg_pool import ConnectionPool

from arxivlens.config import settings

_pool: ConnectionPool | None = None


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            settings().db_url,
            min_size=1,
            max_size=10,
            open=True,
        )
    return _pool


@contextmanager
def conn() -> Iterator[psycopg.Connection]:
    pool = get_pool()
    with pool.connection() as c:
        yield c

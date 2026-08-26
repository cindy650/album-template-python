"""MySQL connections and metadata helpers shared by repositories."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
import logging
import re
from threading import Lock
import time
from typing import Any, Iterator
from urllib.parse import parse_qs, unquote, urlparse

from sqlalchemy import create_pool_from_url


logger = logging.getLogger(__name__)

TRANSIENT_MYSQL_ERROR_CODES = {
    2003,  # Cannot connect to server.
    2006,  # Server has gone away.
    2013,  # Connection lost during query.
    2055,  # Lost connection during handshake.
}

_pools: dict[tuple[Any, ...], Any] = {}
_pools_lock = Lock()


class Row(dict):
    """Dictionary row with optional numeric access for aggregate queries."""

    def __init__(self, values: dict[str, Any]):
        super().__init__(values)
        self._columns = tuple(values)

    def __getitem__(self, key):
        if isinstance(key, int):
            return super().__getitem__(self._columns[key])
        return super().__getitem__(key)


def _named_parameters(sql: str, params: Mapping[str, Any]) -> str:
    result: list[str] = []
    index = 0
    quote = None
    while index < len(sql):
        char = sql[index]
        if quote is not None:
            result.append(char)
            if char == "\\" and index + 1 < len(sql):
                index += 1
                result.append(sql[index])
            elif char == quote:
                if index + 1 < len(sql) and sql[index + 1] == quote:
                    index += 1
                    result.append(sql[index])
                else:
                    quote = None
            index += 1
            continue
        if char in {"'", '"', "`"}:
            quote = char
            result.append(char)
            index += 1
            continue
        if char == ":" and index + 1 < len(sql):
            match = re.match(r"[A-Za-z_][A-Za-z0-9_]*", sql[index + 1 :])
            if match and match.group(0) in params:
                name = match.group(0)
                result.append(f"%({name})s")
                index += len(name) + 1
                continue
        result.append(char)
        index += 1
    return "".join(result)


def _upsert_clause(match: re.Match) -> str:
    assignments = re.sub(
        r"excluded\.([A-Za-z_][\w]*)",
        r"VALUES(\1)",
        match.group(2),
        flags=re.I,
    )
    return "ON DUPLICATE KEY UPDATE " + assignments


def _mysql_sql(sql: str, params=()) -> str:
    """Normalize portable repository statements for MySQL.

    Repositories still contain a small number of SQLite-oriented transaction
    statements.  Translate those at the database boundary so callers do not
    need to know which SQL dialect is active.
    """
    sql = sql.strip()
    sql = re.sub(r"^BEGIN\s+IMMEDIATE$", "START TRANSACTION", sql, flags=re.I)
    sql = re.sub(r"INSERT\s+OR\s+IGNORE\s+INTO", "INSERT IGNORE INTO", sql, flags=re.I)
    sql = re.sub(r"CREATE\s+(UNIQUE\s+)?INDEX\s+IF\s+NOT\s+EXISTS", r"CREATE \1INDEX", sql, flags=re.I)
    sql = re.sub(r"\s+COLLATE\s+NOCASE", "", sql, flags=re.I)
    sql = re.sub(r"ORDER BY\s+rowid", "ORDER BY id", sql, flags=re.I)
    sql = re.sub(r"\bdatetime\(([^()]*)\)", r"CAST(\1 AS DATETIME)", sql, flags=re.I)
    sql = re.sub(r"CAST\(([^()]*)\s+AS\s+TEXT\)", r"CAST(\1 AS CHAR)", sql, flags=re.I)
    sql = re.sub(r"\bAUTOINCREMENT\b", "AUTO_INCREMENT", sql, flags=re.I)
    sql = re.sub(r"\b(TEXT|MEDIUMTEXT|LONGTEXT)\s+NOT\s+NULL\s+DEFAULT\s+'(?:''|[^'])*'", r"\1 NULL", sql, flags=re.I)
    sql = re.sub(r"\b(TEXT|MEDIUMTEXT|LONGTEXT)\s+DEFAULT\s+'(?:''|[^'])*'", r"\1 NULL", sql, flags=re.I)
    sql = re.sub(r"(\b(?:shop|font_name)\s+)TEXT(\s+[^,\n]*\bUNIQUE\b)", r"\1VARCHAR(255)\2", sql, flags=re.I)
    sql = re.sub(r"(\bname\s+)TEXT(\s+NOT\s+NULL)", r"\1VARCHAR(255)\2", sql, flags=re.I)
    sql = re.sub(r"(\bid\s+)TEXT(\s+PRIMARY\s+KEY)", r"\1VARCHAR(255)\2", sql, flags=re.I)
    sql = re.sub(r"(\blayout_id\s+)TEXT(\s+NOT\s+NULL)", r"\1VARCHAR(255)\2", sql, flags=re.I)
    sql = re.sub(r"(\bsize_option_id\s+)TEXT(\s+NOT\s+NULL)", r"\1VARCHAR(255)\2", sql, flags=re.I)
    sql = re.sub(r"(\b(?:order_number|transaction_id)\s+)TEXT(\s*(?:,|$))", r"\1VARCHAR(255)\2", sql, flags=re.I | re.M)
    sql = re.sub(r"\bTEXT\b", "LONGTEXT", sql, flags=re.I)
    sql = re.sub(r"(ON\s+product_names\s*\(\s*name)(\s*\))", r"\1(255)\2", sql, flags=re.I)
    sql = re.sub(r"(ON\s+font_layout_library\s*\(\s*shop_id\s*,\s*sort_key)\s*\)", r"\1(255))", sql, flags=re.I)
    sql = re.sub(r"ON\s+CONFLICT\s*\(([^)]+)\)\s+DO\s+UPDATE\s+SET\s+(.+)$", _upsert_clause, sql, flags=re.I | re.S)
    if isinstance(params, Mapping):
        sql = _named_parameters(sql, params)
    return sql.replace("?", "%s")


def _prepare_sql(sql: str, params=()) -> str:
    """Backward-compatible internal alias for the MySQL SQL normalizer."""
    return _mysql_sql(sql, params)


class Cursor:
    def __init__(self, cursor):
        self._cursor = cursor
        self.lastrowid = getattr(cursor, "lastrowid", None)

    def execute(self, sql: str, params=()):
        sql = _prepare_sql(sql, params)
        try:
            self._cursor.execute(sql, params)
        except Exception as exc:
            if sql.lstrip().upper().startswith("CREATE INDEX") and "DUPLICATE" in str(exc).upper():
                return self
            raise
        self.lastrowid = getattr(self._cursor, "lastrowid", None)
        return self

    def executemany(self, sql: str, seq):
        self._cursor.executemany(_prepare_sql(sql), seq)
        self.lastrowid = getattr(self._cursor, "lastrowid", None)
        return self

    def fetchone(self):
        return self._wrap(self._cursor.fetchone())

    def fetchall(self):
        return [self._wrap(row) for row in self._cursor.fetchall()]

    def __iter__(self):
        return iter(self.fetchall())

    @staticmethod
    def _wrap(row):
        if row is None or isinstance(row, Row):
            return row
        return Row(dict(row))


class Connection:
    mysql = True

    def __init__(self, connection):
        self._connection = connection

    def execute(self, sql: str, params=()):
        return Cursor(self._connection.cursor()).execute(sql, params)

    def executemany(self, sql: str, seq):
        return Cursor(self._connection.cursor()).executemany(sql, seq)

    def commit(self):
        self._connection.commit()

    def rollback(self):
        self._connection.rollback()

    def close(self):
        self._connection.close()


def _mysql_config(target: str) -> dict[str, Any]:
    parsed = urlparse(target.replace("mysql+pymysql://", "mysql://", 1))
    if parsed.scheme != "mysql":
        raise ValueError("数据库仅支持 mysql:// 或 mysql+pymysql:// URL")
    query = parse_qs(parsed.query)
    database = parsed.path.lstrip("/")
    if not database:
        raise ValueError("MySQL URL 必须包含数据库名称")
    return {
        "host": parsed.hostname or "127.0.0.1",
        "port": parsed.port or 3306,
        "user": unquote(parsed.username or "root"),
        "password": unquote(parsed.password or ""),
        "database": database,
        "charset": query.get("charset", ["utf8mb4"])[0],
        "autocommit": False,
    }


def _open_mysql_connection(
    target: Any,
    *,
    retry_attempts: int,
    retry_delay_seconds: float,
    connect_timeout_seconds: int,
):
    if not isinstance(target, str):
        raise ValueError("数据库连接必须使用 MySQL URL")
    try:
        import pymysql
        from pymysql.cursors import DictCursor
    except ImportError as exc:
        raise RuntimeError("MySQL 模式需要安装 PyMySQL：pip install PyMySQL") from exc
    retry_attempts = max(1, int(retry_attempts))
    retry_delay_seconds = max(0.0, float(retry_delay_seconds))
    connect_timeout_seconds = max(1, int(connect_timeout_seconds))
    config = _mysql_config(target)
    config["cursorclass"] = DictCursor
    config["connect_timeout"] = connect_timeout_seconds

    for attempt in range(1, retry_attempts + 1):
        try:
            return pymysql.connect(**config)
        except pymysql.OperationalError as exc:
            error_code = exc.args[0] if exc.args else None
            if (
                error_code not in TRANSIENT_MYSQL_ERROR_CODES
                or attempt == retry_attempts
            ):
                raise
            delay = retry_delay_seconds * (2 ** (attempt - 1))
            logger.warning(
                "MySQL connection attempt %d/%d failed with error %s; "
                "retrying in %.1f seconds",
                attempt,
                retry_attempts,
                error_code,
                delay,
            )
            time.sleep(delay)

    raise RuntimeError("MySQL connection retry loop exited unexpectedly")


def connect(
    target: Any,
    *,
    retry_attempts: int | None = None,
    retry_delay_seconds: float | None = None,
    connect_timeout_seconds: int | None = None,
    pool_size: int | None = None,
    pool_max_overflow: int | None = None,
    pool_timeout_seconds: float | None = None,
    pool_recycle_seconds: int | None = None,
    pool_pre_ping: bool | None = None,
) -> Connection:
    if not isinstance(target, str):
        raise ValueError("数据库连接必须使用 MySQL URL")

    from backend.config import settings

    retry_attempts = (
        settings.mysql_connect_retry_attempts
        if retry_attempts is None
        else retry_attempts
    )
    retry_delay_seconds = (
        settings.mysql_connect_retry_delay_seconds
        if retry_delay_seconds is None
        else retry_delay_seconds
    )
    connect_timeout_seconds = (
        settings.mysql_connect_timeout_seconds
        if connect_timeout_seconds is None
        else connect_timeout_seconds
    )
    pool_size = settings.mysql_pool_size if pool_size is None else pool_size
    pool_max_overflow = (
        settings.mysql_pool_max_overflow
        if pool_max_overflow is None
        else pool_max_overflow
    )
    pool_timeout_seconds = (
        settings.mysql_pool_timeout_seconds
        if pool_timeout_seconds is None
        else pool_timeout_seconds
    )
    pool_recycle_seconds = (
        settings.mysql_pool_recycle_seconds
        if pool_recycle_seconds is None
        else pool_recycle_seconds
    )
    pool_pre_ping = (
        settings.mysql_pool_pre_ping
        if pool_pre_ping is None
        else pool_pre_ping
    )

    retry_attempts = max(1, int(retry_attempts))
    retry_delay_seconds = max(0.0, float(retry_delay_seconds))
    connect_timeout_seconds = max(1, int(connect_timeout_seconds))
    pool_size = max(1, int(pool_size))
    pool_max_overflow = max(0, int(pool_max_overflow))
    pool_timeout_seconds = max(0.1, float(pool_timeout_seconds))
    pool_recycle_seconds = max(1, int(pool_recycle_seconds))
    pool_pre_ping = bool(pool_pre_ping)

    pool_key = (
        target,
        retry_attempts,
        retry_delay_seconds,
        connect_timeout_seconds,
        pool_size,
        pool_max_overflow,
        pool_timeout_seconds,
        pool_recycle_seconds,
        pool_pre_ping,
    )
    with _pools_lock:
        pool = _pools.get(pool_key)
        if pool is None:
            pool = create_pool_from_url(
                "mysql+pymysql://",
                creator=lambda: _open_mysql_connection(
                    target,
                    retry_attempts=retry_attempts,
                    retry_delay_seconds=retry_delay_seconds,
                    connect_timeout_seconds=connect_timeout_seconds,
                ),
                pool_size=pool_size,
                max_overflow=pool_max_overflow,
                timeout=pool_timeout_seconds,
                recycle=pool_recycle_seconds,
                pre_ping=pool_pre_ping,
            )
            _pools[pool_key] = pool
    return Connection(pool.connect())


def dispose_pools():
    with _pools_lock:
        pools = list(_pools.values())
        _pools.clear()
    for pool in pools:
        pool.dispose()


@contextmanager
def connection_scope(target: Any) -> Iterator[Connection]:
    connection = connect(target)
    try:
        yield connection
    except Exception:
        connection.rollback()
        raise
    else:
        connection.commit()
    finally:
        connection.close()


def table_names(connection) -> list[str]:
    return [
        row["name"]
        for row in connection.execute(
            "SELECT TABLE_NAME AS name FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_TYPE = 'BASE TABLE'"
        ).fetchall()
    ]


def table_exists(connection, table: str) -> bool:
    return connection.execute(
        "SELECT 1 AS present FROM information_schema.TABLES "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = ?",
        (table,),
    ).fetchone() is not None


def table_columns(connection, table: str):
    return connection.execute(
        "SELECT ORDINAL_POSITION - 1 AS cid, COLUMN_NAME AS name, "
        "COLUMN_TYPE AS type, (IS_NULLABLE = 'NO') AS notnull, "
        "COLUMN_DEFAULT AS dflt_value, (COLUMN_KEY = 'PRI') AS pk "
        "FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = ? "
        "ORDER BY ORDINAL_POSITION",
        (table,),
    ).fetchall()


def foreign_keys(connection, table: str):
    return connection.execute(
        "SELECT kcu.REFERENCED_TABLE_NAME AS `table`, kcu.COLUMN_NAME AS `from`, "
        "kcu.REFERENCED_COLUMN_NAME AS `to`, rc.DELETE_RULE AS on_delete, "
        "rc.UPDATE_RULE AS on_update "
        "FROM information_schema.KEY_COLUMN_USAGE kcu "
        "JOIN information_schema.REFERENTIAL_CONSTRAINTS rc "
        "ON rc.CONSTRAINT_SCHEMA = kcu.CONSTRAINT_SCHEMA "
        "AND rc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME "
        "WHERE kcu.TABLE_SCHEMA = DATABASE() AND kcu.TABLE_NAME = ? "
        "AND kcu.REFERENCED_TABLE_NAME IS NOT NULL",
        (table,),
    ).fetchall()


def json_array_contains_sql(connection, column: str) -> str:
    return f"JSON_CONTAINS({column}, JSON_QUOTE(?), '$')"


def json_array_contains_any_sql(connection, column: str, value_count: int) -> str:
    if value_count <= 0:
        return "0"
    return "(" + " OR ".join(
        f"JSON_CONTAINS({column}, JSON_QUOTE(?), '$')"
        for _ in range(value_count)
    ) + ")"

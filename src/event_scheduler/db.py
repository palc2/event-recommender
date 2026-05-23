from pathlib import Path

import clickhouse_connect
from clickhouse_connect.driver import Client

from event_scheduler.config import settings

_client: Client | None = None


def _connect(**overrides) -> Client:
    kwargs = {
        "host": settings.clickhouse_host,
        "port": settings.clickhouse_port,
        "username": settings.clickhouse_user,
        "password": settings.clickhouse_password,
        "secure": settings.clickhouse_secure,
    }
    kwargs.update(overrides)
    return clickhouse_connect.get_client(**kwargs)


def get_client() -> Client:
    global _client
    if _client is None:
        _client = _connect(database=settings.clickhouse_database)
    return _client


def run_migrations() -> None:
    client = _connect()
    migrations_dir = Path(__file__).parent / "migrations"
    for sql_file in sorted(migrations_dir.glob("*.sql")):
        statements = sql_file.read_text().split(";")
        for stmt in statements:
            stmt = stmt.strip()
            if stmt:
                client.command(stmt)

import os
from pathlib import Path

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

pool = ConnectionPool(
    os.environ.get("DATABASE_URL", "postgresql://annotation:annotation@localhost:5432/annotation"),
    min_size=1,
    max_size=int(os.environ.get("DB_POOL_SIZE", "10")),
    timeout=3,
    kwargs={"row_factory": dict_row, "options": "-c statement_timeout=5000 -c lock_timeout=2000"},
    open=False,
)


def migrate():
    pool.open(wait=True)
    try:
        with pool.connection() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(56371825)")
            conn.execute(Path(__file__).with_name("schema.sql").read_text())
    finally:
        pool.close()


if __name__ == "__main__":
    migrate()

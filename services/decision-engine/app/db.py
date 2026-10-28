"""psycopg3 connection helper — supports DATABASE_URL or discrete PGHOST/… vars."""
import os
import psycopg


def get_connection_kwargs() -> dict:
    url = os.environ.get("DATABASE_URL")
    if url:
        return {"conninfo": url}
    return {
        "conninfo": (
            f"host={os.environ.get('PGHOST', 'localhost')} "
            f"port={os.environ.get('PGPORT', '5432')} "
            f"user={os.environ.get('PGUSER', 'postgres')} "
            f"password={os.environ.get('PGPASSWORD', '')} "
            f"dbname={os.environ.get('PGDATABASE', 'canopy')}"
        )
    }


def connect(**extra) -> psycopg.Connection:
    kwargs = get_connection_kwargs()
    kwargs.update(extra)
    return psycopg.connect(**kwargs)

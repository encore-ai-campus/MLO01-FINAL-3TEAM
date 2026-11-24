from contextlib import contextmanager

from django.db import connection, transaction
from psycopg2.extensions import parse_dsn


def database_config(dsn):
    params = parse_dsn(dsn)
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": params.pop("dbname", "voc"),
        "USER": params.pop("user", ""),
        "PASSWORD": params.pop("password", ""),
        "HOST": params.pop("host", ""),
        "PORT": params.pop("port", ""),
        "OPTIONS": {"connect_timeout": 5, **params},
    }


@contextmanager
def read_snapshot():
    # Staff visibility, the row count and page contents must agree within one request.
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        yield cursor


def fetch_rows(cursor):
    columns = [column[0] for column in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]

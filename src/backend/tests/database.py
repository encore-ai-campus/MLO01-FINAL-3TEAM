from contextlib import contextmanager
from unittest.mock import patch

from django.db import connection

from config.database import database_config


@contextmanager
def use_database(dsn):
    """Point the desk at the disposable fixture database for these requests."""
    connection.close()
    try:
        with patch.dict(connection.settings_dict, database_config(dsn)):
            yield
    finally:
        connection.close()

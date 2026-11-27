"""Write observable milestones independently of the training transaction."""

import logging
import os
from contextlib import contextmanager

import psycopg
from psycopg.types.json import Jsonb

logger = logging.getLogger(__name__)


class Progress:
    def __init__(self, ref=None, node_id=None):
        self.ref = ref
        self.node_id = node_id
        self.connection = None
        self.enabled = bool(ref and node_id)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        if self.connection is not None:
            self.connection.close()

    def write(self, operation, state, *, reason=None, paths=None):
        if not self.enabled:
            return
        try:
            if self.connection is None:
                self.connection = psycopg.connect(
                    os.environ["DATABASE_URL"], autocommit=True, connect_timeout=2,
                    options="-c statement_timeout=2000",
                )
            self.connection.execute("""
                INSERT INTO voc.training_progress (workflow_ref,node_id,operation,state,reason,paths)
                VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT (workflow_ref,node_id,operation) DO UPDATE
                SET state=excluded.state,reason=excluded.reason,paths=excluded.paths,updated_at=clock_timestamp()
            """, (self.ref, self.node_id, operation, state, reason, Jsonb(paths or {})))
        except psycopg.Error:
            self.enabled = False
            logger.warning("Training progress could not be recorded for %s", self.ref)

    @contextmanager
    def stage(self, operation):
        result = {"state": "completed"}
        self.write(operation, "requested")
        self.write(operation, "running")
        try:
            yield result
        except Exception as error:
            reason = ("timeout" if isinstance(error, TimeoutError) else
                      "database" if isinstance(error, psycopg.Error) else
                      "invalid_data" if isinstance(error, ValueError) else "operation")
            self.write(operation, "failed", reason=reason)
            raise
        else:
            self.write(operation, **result)


NO_PROGRESS = Progress()

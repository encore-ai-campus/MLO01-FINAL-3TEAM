"""Durable capture and provider checkpoints shared by the three Compose workers."""
import json
import sqlite3
from contextlib import closing, contextmanager
from typing import NamedTuple

from .events import archive_path, event_id, now, source_body_changed


class Scope(NamedTuple):
    company: str
    channel: str
    connection: str
    source: str


class Checkpoint(NamedTuple):
    position: str
    phase: str = "active"


_SCOPE = "company_id=? AND channel=? AND connection_id=? AND scope_id=?"


class CaptureStore:
    def __init__(self, root):
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / "capture.sqlite3"
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS capture_outbox (
                    event_id TEXT PRIMARY KEY, company_id TEXT NOT NULL, channel TEXT NOT NULL,
                    connection_id TEXT NOT NULL, source_message_id TEXT NOT NULL,
                    captured_at TEXT NOT NULL, envelope TEXT, published_at TEXT,
                    UNIQUE(company_id,channel,connection_id,source_message_id),
                    CHECK ((published_at IS NULL) = (envelope IS NOT NULL))
                );
                CREATE INDEX IF NOT EXISTS waiting_for_publish ON capture_outbox(captured_at, event_id)
                    WHERE published_at IS NULL;
                CREATE TABLE IF NOT EXISTS collector_checkpoint (
                    company_id TEXT, channel TEXT, connection_id TEXT, scope_id TEXT,
                    position TEXT NOT NULL, phase TEXT NOT NULL,
                    PRIMARY KEY(company_id,channel,connection_id,scope_id)
                ) WITHOUT ROWID;
                CREATE TABLE IF NOT EXISTS capture_issue (
                    company_id TEXT, channel TEXT, connection_id TEXT, scope_id TEXT,
                    reason TEXT, reference TEXT, recorded_at TEXT NOT NULL,
                    PRIMARY KEY(company_id,channel,connection_id,scope_id,reason,reference)
                ) WITHOUT ROWID;
            """)
        # A legacy pending file holds the original envelope even if a raw archive
        # already exists. Import before contacting providers; replay is idempotent.
        for path in sorted((root / "pending").glob("*.json")):
            event = json.loads(path.read_bytes())
            identity = [event[k] for k in ("company_id", "channel", "connection_id", "source_message_id")]
            if path.stem != event["event_id"] or event_id(*identity) != path.stem:
                raise ValueError("invalid legacy pending identity")
            self.stage(event)

    @contextmanager
    def connect(self):
        with closing(sqlite3.connect(self.path, timeout=30)) as db:
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db

    def _insert(self, db, event):
        payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        saved = db.execute("""
            INSERT INTO capture_outbox
            (event_id,company_id,channel,connection_id,source_message_id,captured_at,envelope)
            VALUES (?,?,?,?,?,?,?) ON CONFLICT(event_id) DO NOTHING
        """, (event["event_id"], event["company_id"], event["channel"], event["connection_id"],
              event["source_message_id"], event["received_at"], payload)).rowcount == 1
        if not saved and event["channel"] == "discord":
            row = db.execute("SELECT envelope FROM capture_outbox WHERE event_id=?", (event["event_id"],)).fetchone()
            if row[0] is not None:
                first_raw = json.loads(row[0])["raw"]
            else:
                try:
                    first_raw = json.loads(archive_path(self.path.parent, event).read_bytes())
                except FileNotFoundError:
                    # Bronze-confirmed legacy rows can predate the local archive.
                    return False
            if source_body_changed("discord", first_raw, event["raw"]):
                message = first_raw.get("d", first_raw)
                scope = message.get("channel_id") or event["context"].get("channel_id") or event["connection_id"]
                db.execute("""INSERT INTO capture_issue VALUES (?,?,?,?,?,?,?) ON CONFLICT DO NOTHING""",
                           (event["company_id"], "discord", event["connection_id"], scope,
                            "source_body_changed", event["event_id"], now()))
        return saved

    def stage(self, event):
        with self.connect() as db:
            return self._insert(db, event)

    def contains(self, ident):
        with self.connect() as db:
            return db.execute("SELECT 1 FROM capture_outbox WHERE event_id=?", (ident,)).fetchone() is not None

    def pending(self):
        with self.connect() as db:
            row = db.execute("""SELECT envelope FROM capture_outbox WHERE published_at IS NULL
                                ORDER BY captured_at, event_id LIMIT 1""").fetchone()
        return json.loads(row[0]) if row else None

    def published(self, ident):
        with self.connect() as db:
            db.execute("""UPDATE capture_outbox SET published_at=?, envelope=NULL
                          WHERE event_id=? AND published_at IS NULL""", (now(), ident))

    def confirm_bronze(self, rows):
        # A committed Bronze row proves delivery during migration. The timestamp
        # records when that proof was imported, not the original broker ACK time.
        confirmed_at = now()
        with self.connect() as db:
            db.executemany("""
                INSERT INTO capture_outbox
                (event_id,company_id,channel,connection_id,source_message_id,captured_at,published_at)
                VALUES (?,?,?,?,?,?,?) ON CONFLICT(event_id) DO UPDATE
                SET envelope=NULL, published_at=excluded.published_at
                WHERE capture_outbox.published_at IS NULL
            """, ((str(row["event_id"]), row["company_id"], row["channel"], row["connection_id"],
                   row["source_message_id"], row["received_at"].isoformat(), confirmed_at)
                  for row in rows))

    @staticmethod
    def _read(db, scope):
        row = db.execute("SELECT position,phase FROM collector_checkpoint WHERE " + _SCOPE, scope).fetchone()
        return Checkpoint(*row) if row else None

    def checkpoint(self, scope):
        with self.connect() as db:
            return self._read(db, scope)

    def initialize(self, scope, initial):
        with self.connect() as db:
            db.execute("INSERT INTO collector_checkpoint VALUES (?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                       (*scope, *initial))
            return self._read(db, scope)

    def save_page(self, scope, expected, events=(), *, advance=None, issues=()):
        """Only the reader of this exact cursor may commit its page and move it."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if self._read(db, scope) != expected:
                return False
            for event in events:
                self._insert(db, event)
            db.executemany("INSERT INTO capture_issue VALUES (?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                           ((*scope, reason, ref, now()) for reason, ref in issues))
            if advance is not None:
                db.execute("UPDATE collector_checkpoint SET position=?, phase=? WHERE " + _SCOPE,
                           (*advance, *scope))
        return True

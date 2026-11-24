"""Durable handoff between provider callbacks and the raw Kafka producer."""
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

ROOT = Path(os.environ.get("COLLECTOR_DATA", "/data"))
COMPANY = os.environ.get("VOC_COMPANY", "encore")
DISCORD_CHANNEL = os.environ.get("DISCORD_CHANNEL_ID", "")
GMAIL_LABEL = os.environ.get("GMAIL_LABEL", "VOC")


def now():
    return datetime.now(timezone.utc).isoformat()


def event_id(company, channel, connection, message_id):
    identity = json.dumps([company, channel, connection, message_id], separators=(",", ":"))
    return str(uuid5(NAMESPACE_URL, identity))


def make_event(channel, connection, message_id, origin, raw, **context):
    if channel not in {"discord", "gmail"} or not connection or not re.fullmatch(r"[0-9A-Za-z_-]+", message_id):
        raise ValueError("invalid source identity")
    return {"event_id": event_id(COMPANY, channel, connection, message_id),
            "company_id": COMPANY, "channel": channel, "connection_id": connection,
            "source_message_id": message_id, "origin": origin, "received_at": now(),
            "context": context, "raw": raw}


def archive_path(root, event):
    # Separate accounts without exposing mailbox addresses in paths.
    scope = hashlib.sha256(json.dumps([event["company_id"], event["connection_id"]]).encode()).hexdigest()[:24]
    return root / "raw" / event["channel"] / scope / (event["source_message_id"] + ".json")


def atomic_json(path, value, *, replace=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf8", dir=path.parent, delete=False) as tmp:
        tmp_path = Path(tmp.name)
        try:
            json.dump(value, tmp, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            tmp.flush()
            os.fsync(tmp.fileno())
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise
    try:
        if replace:
            os.replace(tmp_path, path)
        else:
            try:
                os.link(tmp_path, path)
            except FileExistsError:
                return False
        return True
    finally:
        tmp_path.unlink(missing_ok=True)


def pending_path(root, event):
    return root / "pending" / (event["event_id"] + ".json")


def already_received(root, event):
    return archive_path(root, event).is_file() or pending_path(root, event).is_file()


def stage(root, event):
    if already_received(root, event):
        return False
    return atomic_json(pending_path(root, event), event)


def status(role, state, **details):
    atomic_json(ROOT / "status" / (role + ".json"), {"state": state, "at": now(), **details}, replace=True)

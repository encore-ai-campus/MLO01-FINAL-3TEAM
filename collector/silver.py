"""Persist the typed Silver v4 contract against its immutable Bronze source."""
from datetime import datetime
import json
import re

from psycopg.types.json import Jsonb

from .events import event_id

VERSION = 4
IDENTITY = ("company_id", "channel", "connection_id", "source_message_id")
COLUMNS = ("schema_version", "event_id", *IDENTITY, "source_conversation_id", "source_channel_id",
           "source_channel_name", "reply_to_message_id", "message_kind", "sender_ref", "sender_display_name",
           "title", "body_text", "body_version", "occurred_at", "received_at", "source_metadata")
KINDS = {"customer_inquiry", "staff_reply", "other", "unknown"}
OPTIONAL_TEXT = ("source_conversation_id", "source_channel_id", "source_channel_name", "reply_to_message_id",
                 "sender_ref", "sender_display_name", "title")
REQUEST_ROWS = """SELECT b.event_id::text, b.company_id, b.channel, b.connection_id, b.source_message_id,
                         b.origin, b.received_at, b.context, b.raw_payload AS raw,
                         o.body_version, o.verified_message_kind
                  FROM voc.standardize_outbox o JOIN voc.bronze_messages b USING(event_id)
                  WHERE o.target_schema_version = 4"""


class SilverError(ValueError):
    """A fixed, payload-free reason that can be recorded for review."""


def timestamp(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z", value):
        raise SilverError("invalid Silver UTC timestamp")
    try:
        return datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise SilverError("invalid Silver UTC timestamp") from exc


def validate(record):
    if record.keys() != set(COLUMNS) or type(record["schema_version"]) is not int or record["schema_version"] != VERSION:
        raise SilverError("invalid Silver v4 contract")
    if any(not isinstance(record[k], str) or not record[k].strip() for k in (*IDENTITY, "event_id", "body_text")):
        raise SilverError("invalid Silver identity or body")
    if record["channel"] not in ("gmail", "discord") or record["event_id"] != event_id(*(record[k] for k in IDENTITY)):
        raise SilverError("Silver source identity mismatch")
    if not isinstance(record["message_kind"], str) or record["message_kind"] not in KINDS:
        raise SilverError("invalid Silver message kind")
    if type(record["body_version"]) is not int or record["body_version"] < 1:
        raise SilverError("invalid Silver body version")
    for key in OPTIONAL_TEXT:
        value = record[key]
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise SilverError("invalid Silver text field")
    record["received_at"] = timestamp(record["received_at"])
    if record["occurred_at"] is not None:
        record["occurred_at"] = timestamp(record["occurred_at"])
    metadata = record["source_metadata"]
    if record["channel"] == "discord":
        if metadata != {} or record["source_channel_id"] is None or record["title"] is not None:
            raise SilverError("invalid Discord Silver metadata")
    elif (not isinstance(metadata, dict) or metadata.keys() != {"label_ids", "to_addresses"}
          or any(not isinstance(values, list) or not all(isinstance(item, str) for item in values)
                 for values in metadata.values()) or any(record[k] is not None for k in
                                                         ("source_channel_id", "source_channel_name", "reply_to_message_id"))):
        raise SilverError("invalid Gmail Silver metadata")


def discord_message(source):
    raw = source["raw"]
    return raw.get("d") if isinstance(raw, dict) and source["origin"] == "discord_gateway" else raw


def known_root(cn, source):
    if source["channel"] != "discord":
        return None
    message = discord_message(source)
    if (not isinstance(message, dict) or not isinstance(message.get("channel_id"), str)
            or not isinstance(message.get("message_reference"), dict)):
        return None
    # Only small routing fields are passed to the same SQL rule used by late-parent repair.
    routing = {k: message[k] for k in ("type", "guild_id", "channel_id", "message_reference") if k in message}
    row = cn.execute("""SELECT source_conversation_id FROM voc.silver_messages
                        WHERE channel='discord' AND company_id=%s AND connection_id=%s AND source_channel_id=%s
                          AND source_message_id=voc.discord_parent(%s, %s, %s)""",
                     (source["company_id"], source["connection_id"], message.get("channel_id"), Jsonb(routing),
                      source["connection_id"], message.get("channel_id"))).fetchone()
    return row["source_conversation_id"] if row else None


def current_kind(source, record):
    """Use the current confirmation even if this Kafka response predates its change."""
    confirmed = source["verified_message_kind"] or "unknown"
    if source["channel"] == "discord":
        message = discord_message(source)
        author = message.get("author") or {}
        if (isinstance(author, dict) and (author.get("bot") is True or author.get("system") is True)
                or message.get("webhook_id") or message.get("type") not in (None, 0, 19)):
            return "other"
        return confirmed
    raw = source["raw"]
    labels = raw.get("labelIds") or []
    part = raw.get("payload")
    if not isinstance(part, dict):
        part = {}
    if not isinstance(labels, list):
        labels = []
    automatic = str(part.get("mimeType", "")).lower() == "multipart/report"
    for header in part.get("headers") or []:
        if isinstance(header, dict) and str(header.get("name", "")).lower() == "auto-submitted":
            token = re.match(r"^\s*([\w-]+)(?:[;\s(]|$)", str(header.get("value", "")), re.ASCII)
            if token and token[1].lower() != "no":
                automatic = True
                break
    if automatic or "DRAFT" in labels:
        return "other"
    # sender_ref is the single verified From mailbox extracted by the MIME normalizer.
    if "SENT" in labels or (record["sender_ref"] or "").casefold() == source["connection_id"].casefold():
        return "staff_reply"
    return confirmed


def check_conversation(cn, source, record):
    if source["channel"] == "gmail":
        if not isinstance(source["raw"], dict) or source["raw"].get("id") != record["source_message_id"]:
            raise SilverError("Gmail message ID mismatch")
        if not record["source_conversation_id"] or record["source_conversation_id"] != source["raw"].get("threadId"):
            raise SilverError("Gmail thread mismatch")
        return
    message = discord_message(source)
    room = record["source_channel_id"]
    if not isinstance(message, dict) or message.get("id") != record["source_message_id"]:
        raise SilverError("Discord message ID mismatch")
    if (room != message.get("channel_id") or message.get("guild_id") not in (None, source["connection_id"])
            or not isinstance(source["context"], dict) or source["context"].get("channel_id") not in (None, room)):
        raise SilverError("Discord room mismatch")
    # Child insertion and parent arrival must observe one another even with concurrent consumers.
    scope = json.dumps([source["company_id"], source["connection_id"], room])
    cn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (scope,))
    ref = message.get("message_reference") or {}
    if not isinstance(ref, dict):
        raise SilverError("Discord parent mismatch")
    parent = ref.get("message_id") if (message.get("type") in (None, 19) and ref.get("type") in (None, 0)) else None
    if record["reply_to_message_id"] != parent or parent == record["source_message_id"]:
        raise SilverError("Discord parent mismatch")
    thread = source["context"].get("is_thread") is True
    root = room if thread else (known_root(cn, source) if parent else record["source_message_id"])
    # A parent can arrive between the Flink request and this transaction.
    if record["source_conversation_id"] != root and not (parent and not thread and record["source_conversation_id"] is None):
        raise SilverError("Discord conversation mismatch")
    record["source_conversation_id"] = root


def attach_descendants(cn, record):
    """Connect previously orphaned replies to this message, including reply-to-reply chains."""
    cn.execute("""WITH RECURSIVE connected(source_message_id) AS (
                      VALUES (%(source_message_id)s::text)
                      UNION
                      SELECT child.source_message_id FROM connected parent
                      JOIN voc.silver_messages child ON child.reply_to_message_id=parent.source_message_id
                      JOIN voc.bronze_messages b ON b.event_id=child.event_id
                      WHERE child.channel='discord' AND child.company_id=%(company_id)s
                        AND child.connection_id=%(connection_id)s AND child.source_channel_id=%(source_channel_id)s
                        AND child.source_conversation_id IS NULL
                        AND voc.discord_parent(CASE WHEN b.origin='discord_gateway' THEN b.raw_payload->'d'
                                                    ELSE b.raw_payload END, b.connection_id, child.source_channel_id)
                            = parent.source_message_id
                  ) UPDATE voc.silver_messages s SET source_conversation_id=%(source_conversation_id)s
                    FROM connected c WHERE s.source_message_id=c.source_message_id AND s.channel='discord'
                      AND s.company_id=%(company_id)s AND s.connection_id=%(connection_id)s
                      AND s.source_channel_id=%(source_channel_id)s AND s.source_conversation_id IS NULL""", record)


def store_silver(cn, record):
    validate(record)
    source = cn.execute(REQUEST_ROWS + " AND o.event_id=%s FOR UPDATE OF o", (record["event_id"],)).fetchone()
    if not source or any(record[k] != source[k] for k in (*IDENTITY, "received_at")):
        raise SilverError("Silver identity or first receipt differs from Bronze")
    version = record["body_version"]
    if version < source["body_version"]:
        return
    if version > source["body_version"]:
        raise SilverError("unapproved Silver body version")
    check_conversation(cn, source, record)
    existing = cn.execute("""SELECT body_version, body_text, message_kind, source_conversation_id FROM voc.silver_messages
                             WHERE event_id=%s AND schema_version=4""", (record["event_id"],)).fetchone()
    if existing and version == existing["body_version"] and record["body_text"] != existing["body_text"]:
        raise SilverError("different body for the same Silver body version")
    kind = current_kind(source, record)
    record["message_kind"] = kind
    if not existing:
        values = {**record, "source_metadata": Jsonb(record["source_metadata"])}
        cn.execute("INSERT INTO voc.silver_messages (" + ", ".join(COLUMNS) + ") VALUES ("
                   + ", ".join("%(" + key + ")s" for key in COLUMNS) + ")", values)
    else:
        root = existing["source_conversation_id"] or record["source_conversation_id"]
        if version > existing["body_version"] and record["body_text"] == existing["body_text"]:
            raise SilverError("unchanged body for a new Silver body version")
        if (version, kind, root) == (existing["body_version"], existing["message_kind"], existing["source_conversation_id"]):
            return
        cn.execute("""UPDATE voc.silver_messages SET body_text=%s, body_version=%s, message_kind=%s, source_conversation_id=%s
                      WHERE event_id=%s AND schema_version=4""",
                   (record["body_text"], version, kind, root, record["event_id"]))
    if record["channel"] == "discord" and record["source_conversation_id"] and (not existing or not existing["source_conversation_id"]):
        attach_descendants(cn, record)

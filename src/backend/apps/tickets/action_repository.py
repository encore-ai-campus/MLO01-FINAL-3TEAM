import base64
import json

from apps.accounts.repository import TICKET_READ
from apps.collaboration import repository as inbox_repository

from config.database import fetch_rows

from .status import STATUS_SQL


CONDUCT = f"""
    ({TICKET_READ} AND EXISTS (
        SELECT FROM staff_permission sp JOIN permission p USING (permission_id)
        WHERE sp.staff_user_id=s.staff_user_id AND p.code='ticket.handle'
    ))
"""
MESSAGE_FIELDS = """
    ticket_message_id, ticket_id, staff_user_id, sender_type,
    message_type, body, delivery_status, created_at
"""


def can_conduct(cursor, actor):
    cursor.execute(f"SELECT {CONDUCT} FROM staff_user s WHERE s.staff_user_id=%s", (actor["staff_user_id"],))
    return cursor.fetchone()[0]


def lock_ticket(cursor, actor, ticket_id):
    cursor.execute(f"""
        SELECT t.ticket_id, {STATUS_SQL} AS status, t.assignee_id, t.csv_import_kind
        FROM ticket t
        WHERE t.ticket_id=%s AND t.company_id=%s
          AND EXISTS (SELECT FROM visible_ticket v WHERE v.ticket_id=t.ticket_id)
        FOR UPDATE OF t
    """, (ticket_id, actor["company_id"]))
    return next(iter(fetch_rows(cursor)), None)


def has_unsettled_reply(cursor, ticket_id):
    cursor.execute("""
        SELECT 1 FROM ticket_message WHERE ticket_id=%s AND delivery_status IN ('sending','unknown')
    """, (ticket_id,))
    return cursor.fetchone() is not None


def reply_source(cursor, ticket_id):
    cursor.execute("""
        SELECT t.company_id, s.source_channel_type AS source_kind, s.source_connection_id AS connection_id, s.source_message_id,
               s.source_conversation_id, s.source_channel_id, s.sender_ref AS reply_address,
               s.title AS original_subject, (b.raw_payload_json #> '{payload,headers}')::text AS headers
        FROM ticket t JOIN voc.silver_message s ON s.event_id=t.source_event_id AND s.schema_version=4
        JOIN voc.bronze_message b ON b.event_id=s.event_id
        JOIN voc.ticket_source_connection route ON
            (route.source_company_id, route.source_channel_type, route.source_connection_id, route.source_channel_id,
             route.company_id, route.channel_id) =
            (s.source_company_id, s.source_channel_type, s.source_connection_id, COALESCE(s.source_channel_id,''), t.company_id, t.channel_id)
        WHERE t.ticket_id=%s AND s.message_kind='customer_inquiry'
    """, (ticket_id,))
    source = next(iter(fetch_rows(cursor)), None)
    if source and source["source_kind"] == "gmail":
        headers = json.loads(source.pop("headers") or "[]")
        values = {
            header["name"].casefold(): header["value"]
            for header in (headers if isinstance(headers, list) else [])
            if isinstance(header, dict)
            and isinstance(header.get("name"), str) and isinstance(header.get("value"), str)
        }
        source["rfc_message_id"] = values.get("message-id", "").strip()
        source["original_subject"] = source["original_subject"] or ""
    return source


def options(cursor, actor, ticket):
    cursor.execute(f"""
        SELECT s.staff_user_id, COALESCE(s.name,s.email) AS name, tm.name AS team
        FROM staff_user s LEFT JOIN team tm USING (company_id, team_id)
        WHERE s.company_id=%s AND {CONDUCT}
        ORDER BY name,s.staff_user_id
    """, (actor["company_id"],))
    staff = fetch_rows(cursor)
    excluded = {actor["staff_user_id"], ticket["assignee_id"]}
    recipients = [person for person in staff if person["staff_user_id"] not in excluded]
    cursor.execute(f"""
        SELECT ch.review_channel_id,ch.name FROM ticket_review_channel ch
        WHERE ch.company_id=%s AND EXISTS (
            SELECT FROM ticket_review_channel_member m JOIN staff_user s USING (company_id,staff_user_id)
            WHERE m.review_channel_id=ch.review_channel_id AND s.staff_user_id<>%s AND {CONDUCT})
        ORDER BY ch.name,ch.review_channel_id
    """, (actor["company_id"], actor["staff_user_id"]))
    return {"assignment_recipients": staff, "transfer_recipients": recipients, "review_channels": fetch_rows(cursor)}


def quick_replies(cursor, actor, ticket_id):
    cursor.execute("""
        SELECT q.reply_id AS quick_reply_uuid,q.name,q.body,
               CASE WHEN q.owner_id IS NOT NULL THEN 'personal' ELSE 'team' END AS scope
        FROM visible_ticket t
        JOIN quick_reply q USING (company_id,domain_id,category_id)
        JOIN staff_user s ON s.staff_user_id=%s AND s.company_id=t.company_id
        WHERE t.ticket_id=%s
          AND (q.owner_id=s.staff_user_id OR q.team_id=s.team_id)
        ORDER BY q.name,q.reply_id
    """, (actor["staff_user_id"], ticket_id))
    return fetch_rows(cursor)


def add_event(cursor, ticket_id, actor, code, *, request=None, recipient=None, reason=None,
              from_status=None, to_status=None):
    cursor.execute("""
        INSERT INTO ticket_event
            (ticket_id,event_type_id,actor_id,recipient_id,collab_request_id,transfer_reason,from_status,to_status,created_at)
        VALUES (%s,(SELECT event_type_id FROM event_type WHERE code=%s),%s,%s,%s,%s,%s,%s,clock_timestamp())
        RETURNING ticket_event_id
    """, (ticket_id, code, actor["staff_user_id"], recipient, request, reason, from_status, to_status))
    return cursor.fetchone()[0]


def add_request(cursor, ticket_id, actor, recipient, kind, status, memo, channel=None):
    cursor.execute("""
        INSERT INTO collab_request
            (ticket_id,requester_id,recipient_id,request_type,status,memo,review_channel_id,decided_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,CASE WHEN %s='accepted' THEN now() END)
        RETURNING collab_request_id
    """, (ticket_id, actor["staff_user_id"], recipient, kind, status, memo, channel, status))
    return cursor.fetchone()[0]


def add_attachment(cursor, request, attachment):
    content_type = attachment["content_type"]
    content = attachment["content"]
    data = content.encode("utf-8") if content_type == "text/plain" else base64.b64decode(content)
    extension = "txt" if content_type == "text/plain" else content_type.split("/")[1]
    cursor.execute("""
        INSERT INTO collab_request_attachment
            (collab_request_id,original_filename,content_type,storage_key,size_bytes,content)
        VALUES (%s,%s,%s,%s,%s,%s)
    """, (request, attachment.get("filename", f"reference.{extension}"), content_type, f"db:{request}", len(data), data))


def inbox(cursor, actor, kind=None):
    rows = inbox_repository.received(cursor, (actor["company_id"], actor["staff_user_id"]))
    return [row for row in rows if row["kind"] in ("transfer", "review") and (kind is None or row["kind"] == kind)]


def request_attachment(cursor, actor, action_uuid, attachment_id=None):
    cursor.execute(f"""
        SELECT a.original_filename,a.content_type,a.content
        {inbox_repository.REQUEST_FROM}
        JOIN collab_request_attachment a USING (collab_request_id)
        WHERE r.recipient_id=%s AND t.company_id=%s AND r.collab_request_id=%s
          AND a.content IS NOT NULL AND (%s::uuid IS NULL OR a.collab_request_attachment_id=%s)
        ORDER BY a.created_at,a.collab_request_attachment_id LIMIT 1
    """, (actor["staff_user_id"], actor["company_id"], action_uuid, attachment_id, attachment_id))
    return next(iter(fetch_rows(cursor)), None)


def retry_attachments(cursor, ticket_id, staff_uuid, message_uuid):
    cursor.execute("""
        SELECT a.filename, a.content_type, a.content
        FROM ticket_message_attachment a JOIN ticket_message m USING(ticket_message_id)
        WHERE m.ticket_message_id=%s AND m.ticket_id=%s AND m.staff_user_id=%s
          AND m.message_type='reply' AND m.delivery_status='failed'
        ORDER BY a.attachment_id
    """, (message_uuid, ticket_id, staff_uuid))
    return [dict(row, content=bytes(row["content"])) for row in fetch_rows(cursor)]


def save_message_attachment(cursor, message_uuid, file):
    cursor.execute("""
        INSERT INTO ticket_message_attachment(ticket_message_id,filename,content_type,content)
        VALUES (%s,%s,%s,%s)
    """, (message_uuid, file["filename"], file["content_type"], file["content"]))


def visible_message_attachment(cursor, actor, ticket_id, attachment_id, include_memos):
    cursor.execute("""
        SELECT a.filename, a.content_type, a.content
        FROM visible_ticket t JOIN ticket_message m USING(ticket_id)
        JOIN ticket_message_attachment a USING(ticket_message_id)
        WHERE t.company_id=%s AND t.ticket_id=%s AND a.attachment_id=%s
          AND (%s OR m.message_type<>'memo')
    """, (actor["company_id"], ticket_id, attachment_id, include_memos))
    return next(iter(fetch_rows(cursor)), None)

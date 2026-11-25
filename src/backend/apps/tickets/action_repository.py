import base64
import json

from apps.collaboration import repository as inbox_repository

from config.database import fetch_rows


CONDUCT = """
    2 = (
        SELECT count(*)
        FROM staff_permission sp JOIN permission p USING (permission_uuid)
        WHERE sp.staff_user_uuid=s.staff_user_uuid AND p.code IN ('ticket.read','ticket.handle')
    )
"""
MESSAGE_FIELDS = """
    ticket_message_uuid, ticket_uuid, staff_user_uuid, sender_type,
    message_type, body, delivery_status, created_at
"""


def can_conduct(cursor, actor):
    cursor.execute(f"SELECT {CONDUCT} FROM staff_user s WHERE s.staff_user_uuid=%s", (actor["staff_user_uuid"],))
    return cursor.fetchone()[0]


def lock_ticket(cursor, actor, ticket_uuid):
    cursor.execute("""
        SELECT t.ticket_uuid, t.status, t.assignee_uuid, t.csv_import_kind
        FROM ticket t
        WHERE t.ticket_uuid=%s AND t.company_uuid=%s
          AND EXISTS (SELECT FROM visible_ticket v WHERE v.ticket_uuid=t.ticket_uuid)
        FOR UPDATE OF t
    """, (ticket_uuid, actor["company_uuid"]))
    return next(iter(fetch_rows(cursor)), None)


def reply_source(cursor, ticket_uuid):
    cursor.execute("""
        SELECT t.company_uuid, s.channel AS source_kind, s.connection_id, s.source_message_id,
               s.source_conversation_id, s.source_channel_id, s.sender_ref AS reply_address,
               s.title AS original_subject, (b.raw_payload #> '{payload,headers}')::text AS headers
        FROM ticket t JOIN voc.silver_messages s ON s.event_id=t.source_event_id AND s.schema_version=4
        JOIN voc.bronze_messages b ON b.event_id=s.event_id
        JOIN voc.ticket_source_connection route ON
            (route.company_id, route.channel, route.connection_id, route.source_channel_id,
             route.company_uuid, route.channel_uuid) =
            (s.company_id, s.channel, s.connection_id, COALESCE(s.source_channel_id,''), t.company_uuid, t.channel_uuid)
        WHERE t.ticket_uuid=%s AND s.message_kind='customer_inquiry'
    """, (ticket_uuid,))
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
        SELECT s.staff_user_uuid, COALESCE(s.name,s.email) AS name, tm.name AS team
        FROM staff_user s LEFT JOIN team tm USING (company_uuid, team_uuid)
        WHERE s.company_uuid=%s AND s.staff_user_uuid<>%s
          AND s.staff_user_uuid IS DISTINCT FROM %s AND {CONDUCT}
        ORDER BY name,s.staff_user_uuid
    """, (actor["company_uuid"], actor["staff_user_uuid"], ticket["assignee_uuid"]))
    recipients = fetch_rows(cursor)
    cursor.execute(f"""
        SELECT ch.review_channel_uuid,ch.name FROM ticket_review_channel ch
        WHERE ch.company_uuid=%s AND EXISTS (
            SELECT FROM ticket_review_channel_member m JOIN staff_user s USING (company_uuid,staff_user_uuid)
            WHERE m.review_channel_uuid=ch.review_channel_uuid AND s.staff_user_uuid<>%s AND {CONDUCT})
        ORDER BY ch.name,ch.review_channel_uuid
    """, (actor["company_uuid"], actor["staff_user_uuid"]))
    return {"transfer_recipients": recipients, "review_channels": fetch_rows(cursor)}


def quick_replies(cursor, actor, ticket_uuid):
    cursor.execute("""
        SELECT q.reply_uuid AS quick_reply_uuid,q.name,q.body,
               CASE WHEN q.owner_uuid IS NOT NULL THEN 'personal' ELSE 'team' END AS scope
        FROM visible_ticket t
        JOIN quick_reply q USING (company_uuid,domain_uuid,category_uuid)
        JOIN staff_user s ON s.staff_user_uuid=%s AND s.company_uuid=t.company_uuid
        WHERE t.ticket_uuid=%s
          AND (q.owner_uuid=s.staff_user_uuid OR q.team_uuid=s.team_uuid)
        ORDER BY q.name,q.reply_uuid
    """, (actor["staff_user_uuid"], ticket_uuid))
    return fetch_rows(cursor)


def add_event(cursor, ticket_uuid, actor, code, *, request=None, recipient=None, reason=None,
              from_status=None, to_status=None):
    cursor.execute("""
        INSERT INTO ticket_event
            (ticket_uuid,event_type_uuid,actor_uuid,recipient_uuid,collab_request_uuid,transfer_reason,from_status,to_status)
        VALUES (%s,(SELECT event_type_uuid FROM event_type WHERE code=%s),%s,%s,%s,%s,%s,%s)
        RETURNING ticket_event_uuid
    """, (ticket_uuid, code, actor["staff_user_uuid"], recipient, request, reason, from_status, to_status))
    return cursor.fetchone()[0]


def add_request(cursor, ticket_uuid, actor, recipient, kind, status, memo, channel=None):
    cursor.execute("""
        INSERT INTO collab_request
            (ticket_uuid,requester_uuid,recipient_uuid,request_type,status,memo,review_channel_uuid,decided_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,CASE WHEN %s='accepted' THEN now() END)
        RETURNING collab_request_uuid
    """, (ticket_uuid, actor["staff_user_uuid"], recipient, kind, status, memo, channel, status))
    return cursor.fetchone()[0]


def add_attachment(cursor, request, attachment):
    content_type = attachment["content_type"]
    content = attachment["content"]
    data = content.encode("utf-8") if content_type == "text/plain" else base64.b64decode(content)
    extension = "txt" if content_type == "text/plain" else content_type.split("/")[1]
    cursor.execute("""
        INSERT INTO collab_request_attachment
            (collab_request_uuid,original_filename,content_type,storage_key,size_bytes,content)
        VALUES (%s,%s,%s,%s,%s,%s)
    """, (request, f"reference.{extension}", content_type, f"db:{request}", len(data), data))


def inbox(cursor, actor, kind=None):
    rows = inbox_repository.received(cursor, (actor["company_uuid"], actor["staff_user_uuid"]))
    return [row for row in rows if row["kind"] in ("transfer", "review") and (kind is None or row["kind"] == kind)]


def request_attachment(cursor, actor, action_uuid):
    cursor.execute(f"""
        SELECT a.original_filename,a.content_type,a.content
        {inbox_repository.REQUEST_FROM}
        JOIN collab_request_attachment a USING (collab_request_uuid)
        WHERE r.recipient_uuid=%s AND t.company_uuid=%s AND r.collab_request_uuid=%s
          AND a.content IS NOT NULL
        ORDER BY a.created_at,a.collab_request_attachment_uuid LIMIT 1
    """, (actor["staff_user_uuid"], actor["company_uuid"], action_uuid))
    return next(iter(fetch_rows(cursor)), None)

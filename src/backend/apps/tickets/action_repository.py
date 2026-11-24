import json

from config.database import fetch_rows


CONDUCT = """
    2 = (
        SELECT count(*)
        FROM staff_permission sp JOIN permission p USING (permission_uuid)
        WHERE sp.staff_user_uuid=s.staff_user_uuid AND p.code IN ('ticket.read','ticket.conduct')
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
        SELECT q.quick_reply_uuid,q.name,q.body,q.scope FROM visible_ticket t
        JOIN quick_reply q USING (company_uuid,category_uuid)
        JOIN staff_user s ON s.staff_user_uuid=%s AND s.company_uuid=t.company_uuid
        WHERE t.ticket_uuid=%s
          AND ((q.scope='personal' AND q.author_uuid=s.staff_user_uuid) OR
               (q.scope='team' AND EXISTS (SELECT FROM quick_reply_team qt
                WHERE qt.quick_reply_uuid=q.quick_reply_uuid AND qt.company_uuid=t.company_uuid
                  AND qt.team_uuid=s.team_uuid)))
        ORDER BY q.name,q.quick_reply_uuid
    """, (actor["staff_user_uuid"], ticket_uuid))
    return fetch_rows(cursor)


def add_event(cursor, ticket_uuid, actor, kind, *, channel=None, memo=None, attached=None,
              attachment=None, from_status=None, to_status=None):
    cursor.execute("""
        INSERT INTO ticket_action
            (ticket_uuid,actor_uuid,kind,review_channel_uuid,memo,attached_reply,attachment,from_status,to_status)
        VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s) RETURNING action_uuid
    """, (ticket_uuid, actor["staff_user_uuid"], kind, channel, memo, attached, attachment, from_status, to_status))
    return cursor.fetchone()[0]


def inbox(cursor, actor, kind=None):
    cursor.execute("""
        SELECT a.action_uuid,t.ticket_uuid,t.ticket_number,t.title,a.kind,a.memo,a.attached_reply,
               (CASE WHEN a.attachment->>'content_type'='text/plain' THEN a.attachment
                  ELSE a.attachment - 'content' END)::text AS attachment,
               a.created_at,ch.name AS review_channel,COALESCE(s.name,s.email) AS sender
        FROM ticket_action_recipient r JOIN ticket_action a USING (action_uuid)
        JOIN visible_ticket t USING (ticket_uuid)
        JOIN staff_user s ON s.staff_user_uuid=a.actor_uuid AND s.company_uuid=t.company_uuid
        LEFT JOIN ticket_review_channel ch ON ch.review_channel_uuid=a.review_channel_uuid
            AND ch.company_uuid=t.company_uuid
        WHERE r.staff_user_uuid=%s AND t.company_uuid=%s AND a.kind=ANY(%s)
        ORDER BY a.created_at DESC,a.action_uuid LIMIT 100
    """, (actor["staff_user_uuid"], actor["company_uuid"], [kind] if kind else ["transfer", "review"]))
    rows = fetch_rows(cursor)
    for row in rows:
        row["attachment"] = json.loads(row["attachment"] or "null")
    return rows


def request_attachment(cursor, actor, action_uuid):
    cursor.execute("""
        SELECT a.attachment::text
        FROM ticket_action_recipient r JOIN ticket_action a USING (action_uuid)
        JOIN visible_ticket t USING (ticket_uuid)
        WHERE r.staff_user_uuid=%s AND t.company_uuid=%s AND a.action_uuid=%s AND a.attachment IS NOT NULL
    """, (actor["staff_user_uuid"], actor["company_uuid"], action_uuid))
    row = cursor.fetchone()
    return json.loads(row[0]) if row else None

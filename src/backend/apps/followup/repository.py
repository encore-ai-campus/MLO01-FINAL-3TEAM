from config.database import fetch_rows


def add_track(cursor, company, staff, ticket):
    cursor.execute("""
        INSERT INTO followup_track(company_uuid, staff_user_uuid, ticket_uuid)
        VALUES (%s,%s,%s) ON CONFLICT DO NOTHING
    """, (company, staff, ticket))
    return bool(cursor.rowcount)


def remove_track(cursor, company, staff, ticket):
    cursor.execute("""
        DELETE FROM followup_track WHERE company_uuid=%s AND staff_user_uuid=%s AND ticket_uuid=%s
    """, (company, staff, ticket))


def tracks(cursor, company, staff):
    cursor.execute("""
        SELECT t.ticket_uuid, t.ticket_number, t.title, t.status, t.assignee_uuid, f.tracked_at,
               coalesce(s.name,s.email) AS assignee
        FROM followup_track f JOIN visible_ticket t USING(company_uuid,ticket_uuid)
        LEFT JOIN staff_user s ON s.staff_user_uuid=t.assignee_uuid
        WHERE f.company_uuid=%s AND f.staff_user_uuid=%s
        ORDER BY f.tracked_at DESC, t.ticket_uuid
    """, (company, staff))
    return fetch_rows(cursor)


def notifications(cursor, company, staff, *, summary=False):
    query = """
        WITH tracked AS (
            SELECT t.ticket_uuid, t.company_uuid, t.customer_uuid, t.source_event_id,
                   t.ticket_number, t.title, t.status, f.staff_user_uuid, f.tracked_at
            FROM followup_track f JOIN visible_ticket t USING(company_uuid,ticket_uuid)
            WHERE f.company_uuid=%s AND f.staff_user_uuid=%s
        )
        SELECT m.ticket_message_uuid AS event_uuid, t.ticket_uuid, NULL::uuid AS related_ticket_uuid,
               'memo' AS kind, m.created_at AS occurred_at, author.staff_user_uuid AS author_uuid,
               coalesce(author.name,author.email) AS author, m.body, t.ticket_number, t.title
        FROM tracked t JOIN ticket_message m USING(ticket_uuid)
        JOIN staff_user author ON author.staff_user_uuid=m.staff_user_uuid AND author.company_uuid=t.company_uuid
        WHERE m.sender_type='staff' AND m.message_type='memo'
          AND author.staff_user_uuid<>t.staff_user_uuid AND m.created_at>=t.tracked_at
        UNION ALL
        SELECT n.event_id, t.ticket_uuid, next_ticket.ticket_uuid, 'customer_inquiry',
               n.standardized_at, NULL, NULL, n.body_text, t.ticket_number, t.title
        FROM tracked t
        JOIN voc.silver_messages root ON root.event_id=t.source_event_id AND root.schema_version=4
        JOIN voc.silver_messages n ON n.schema_version=4 AND n.message_kind='customer_inquiry'
            AND n.company_id=root.company_id AND n.channel=root.channel AND n.connection_id=root.connection_id
            AND n.source_channel_id IS NOT DISTINCT FROM root.source_channel_id
            AND n.source_conversation_id=root.source_conversation_id
        JOIN visible_ticket next_ticket ON next_ticket.source_event_id=n.event_id
            AND next_ticket.company_uuid=t.company_uuid AND next_ticket.customer_uuid=t.customer_uuid
            AND next_ticket.ticket_uuid<>t.ticket_uuid
        WHERE n.standardized_at>=t.tracked_at
          AND coalesce(n.occurred_at,n.received_at)>coalesce(root.occurred_at,root.received_at)
          -- The closed Gmail thread rule is pending team agreement.
          AND (root.channel<>'gmail' OR t.status<>'답변완료')
    """
    if summary:
        query = "SELECT count(*) AS count, count(DISTINCT ticket_uuid) AS ticket_count FROM (" + query + ") events"
    else:
        query += " ORDER BY occurred_at DESC, event_uuid, ticket_uuid"
    cursor.execute(query, (company, staff))
    rows = fetch_rows(cursor)
    return rows[0] if summary else rows


def source_exists(cursor, company, ticket, chat):
    if ticket:
        cursor.execute("SELECT 1 FROM visible_ticket WHERE company_uuid=%s AND ticket_uuid=%s", (company, ticket))
    else:
        cursor.execute("SELECT 1 FROM chat WHERE company_uuid=%s AND chat_uuid=%s", (company, chat))
    return cursor.fetchone() is not None


def registration_sources(cursor, company, staff, kind, keyword, *, tracking=False):
    if kind == "ticket":
        cursor.execute("""
            SELECT t.ticket_uuid AS source_uuid, t.ticket_number AS label, t.title
            FROM visible_ticket t
            WHERE t.company_uuid=%s AND strpos(lower('#' || t.ticket_number || ' ' || t.title), lower(%s))>0
                AND (NOT %s OR NOT EXISTS (
                    SELECT FROM followup_track f WHERE f.company_uuid=t.company_uuid
                      AND f.ticket_uuid=t.ticket_uuid AND f.staff_user_uuid=%s
                ))
            ORDER BY t.created_at DESC, t.ticket_uuid LIMIT 20
        """, (company, keyword, tracking, staff))
    else:
        cursor.execute("""
            SELECT c.chat_uuid AS source_uuid, left(c.chat_uuid::text, 8) AS label,
                   coalesce(left(first_message.body,100), '1:1 상담') AS title
            FROM chat c
            LEFT JOIN LATERAL (
                SELECT body FROM chat_message WHERE chat_uuid=c.chat_uuid AND sender_type='customer'
                ORDER BY sent_at, chat_message_uuid LIMIT 1
            ) first_message ON true
            WHERE c.company_uuid=%s AND strpos(lower(concat_ws(' ',c.chat_uuid,first_message.body)),lower(%s))>0
            ORDER BY c.waiting_at DESC, c.chat_uuid LIMIT 20
        """, (company, keyword))
    return fetch_rows(cursor)


def add_contact(cursor, company, staff, data):
    cursor.execute("""
        INSERT INTO followup_contact(company_uuid,created_by_uuid,ticket_uuid,chat_uuid,assignee_uuid,reason,scheduled_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING contact_uuid
    """, (company, staff, data["ticket_uuid"], data["chat_uuid"], data["assignee_uuid"],
          data["reason"], data["scheduled_at"]))
    return cursor.fetchone()[0]


def contacts(cursor, company, staff, can_read, can_chat, *, ident=None, filters=None, lock=False):
    query = """
        SELECT c.contact_uuid, c.ticket_uuid, c.chat_uuid, t.ticket_number,
               coalesce(t.title, left(first_message.body, 100)) AS title,
               c.reason, c.scheduled_at, c.assignee_uuid, coalesce(a.name,a.email) AS assignee,
               c.contacted_at, c.completed_by_uuid, coalesce(p.name,p.email) AS completed_by, c.result
        FROM followup_contact c
        LEFT JOIN visible_ticket t USING (company_uuid,ticket_uuid)
        LEFT JOIN LATERAL (
            SELECT body FROM chat_message WHERE chat_uuid=c.chat_uuid AND sender_type='customer'
            ORDER BY sent_at, chat_message_uuid LIMIT 1
        ) first_message ON true
        JOIN staff_user a ON a.staff_user_uuid=c.assignee_uuid
        LEFT JOIN staff_user p ON p.staff_user_uuid=c.completed_by_uuid
        WHERE c.company_uuid=%s AND %s IN (c.created_by_uuid,c.assignee_uuid)
          AND ((%s AND t.ticket_uuid IS NOT NULL) OR (%s AND c.chat_uuid IS NOT NULL))
    """
    params = [company, staff, can_read, can_chat]
    if ident is not None:
        query += " AND c.contact_uuid=%s"
        params.append(ident)
        if lock:
            query += " FOR UPDATE OF c"
    else:
        query += " AND (c.contacted_at IS NOT NULL)=%s"
        params.append(filters["state"] == "completed")
        if filters["source"]:
            query += " AND (c.ticket_uuid IS NOT NULL)=%s"
            params.append(filters["source"] == "ticket")
        if filters["q"]:
            query += """
                AND strpos(lower(concat_ws(' ', '#' || t.ticket_number, c.chat_uuid, t.title, first_message.body,
                                           c.reason, coalesce(a.name,a.email), coalesce(p.name,p.email), c.result)),
                           lower(%s)) > 0
            """
            params.append(filters["q"])
        query += " ORDER BY coalesce(c.contacted_at,c.scheduled_at) DESC, c.contact_uuid"
    cursor.execute(query, params)
    return fetch_rows(cursor)


def complete_contact(cursor, ident, staff, contacted_at, result):
    cursor.execute("""
        UPDATE followup_contact SET completed_by_uuid=%s, contacted_at=%s, result=%s WHERE contact_uuid=%s
    """, (staff, contacted_at, result, ident))


def chat_detail(cursor, company, chat):
    cursor.execute("""
        SELECT chat_uuid, customer_uuid, status, waiting_at, started_at, ended_at, assignee_uuid
        FROM chat WHERE company_uuid=%s AND chat_uuid=%s
    """, (company, chat))
    item = next(iter(fetch_rows(cursor)), None)
    if item:
        cursor.execute("""
            SELECT chat_message_uuid, sender_type, message_type, body, sent_at FROM chat_message
            WHERE chat_uuid=%s ORDER BY sent_at, chat_message_uuid
        """, (chat,))
        item["messages"] = fetch_rows(cursor)
    return item

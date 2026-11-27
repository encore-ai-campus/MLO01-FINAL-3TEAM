from config.database import fetch_rows


def add_track(cursor, company, staff, ticket):
    cursor.execute("""
        INSERT INTO followup_track(company_id, staff_user_id, ticket_id)
        VALUES (%s,%s,%s) ON CONFLICT DO NOTHING
    """, (company, staff, ticket))
    return bool(cursor.rowcount)


def remove_track(cursor, company, staff, ticket):
    cursor.execute("""
        DELETE FROM followup_track WHERE company_id=%s AND staff_user_id=%s AND ticket_id=%s
    """, (company, staff, ticket))
    if cursor.rowcount:
        cursor.execute("""
            UPDATE followup_contact SET reminders_stopped_at=clock_timestamp()
            WHERE company_id=%s AND created_by_id=%s AND ticket_id=%s
              AND reminders_started_at IS NOT NULL AND reminders_stopped_at IS NULL
        """, (company, staff, ticket))


def tracks(cursor, company, staff):
    cursor.execute("""
        SELECT t.ticket_id, t.ticket_number, t.title, t.status, t.assignee_id, f.tracked_at,
               coalesce(s.name,s.email) AS assignee
        FROM followup_track f JOIN visible_ticket t USING(company_id,ticket_id)
        LEFT JOIN staff_user s ON s.staff_user_id=t.assignee_id
        WHERE f.company_id=%s AND f.staff_user_id=%s
        ORDER BY f.tracked_at DESC, t.ticket_id
    """, (company, staff))
    return fetch_rows(cursor)


def notifications(cursor, company, staff, *, summary=False):
    query = """
        WITH tracked AS (
            SELECT t.ticket_id, t.company_id, t.customer_id, t.source_event_id,
                   t.ticket_number, t.title, t.status, f.staff_user_id, f.tracked_at
            FROM followup_track f JOIN visible_ticket t USING(company_id,ticket_id)
            WHERE f.company_id=%s AND f.staff_user_id=%s
        )
        SELECT m.ticket_message_id AS event_uuid, t.ticket_id, NULL::uuid AS related_ticket_uuid,
               'memo' AS kind, m.created_at AS occurred_at, author.staff_user_id AS author_id,
               coalesce(author.name,author.email) AS author, m.body, t.ticket_number, t.title
        FROM tracked t JOIN ticket_message m USING(ticket_id)
        JOIN staff_user author ON author.staff_user_id=m.staff_user_id AND author.company_id=t.company_id
        WHERE m.sender_type='staff' AND m.message_type='memo'
          AND author.staff_user_id<>t.staff_user_id AND m.created_at>=t.tracked_at
        UNION ALL
        SELECT n.event_id, t.ticket_id, next_ticket.ticket_id, 'customer_inquiry',
               n.standardized_at, NULL, NULL, n.body_text, t.ticket_number, t.title
        FROM tracked t
        JOIN voc.silver_message root ON root.event_id=t.source_event_id AND root.schema_version=4
        JOIN voc.silver_message n ON n.schema_version=4 AND n.message_kind='customer_inquiry'
            AND n.source_company_id=root.source_company_id AND n.source_channel_type=root.source_channel_type AND n.source_connection_id=root.source_connection_id
            AND n.source_channel_id IS NOT DISTINCT FROM root.source_channel_id
            AND n.source_conversation_id=root.source_conversation_id
        JOIN visible_ticket next_ticket ON next_ticket.source_event_id=n.event_id
            AND next_ticket.company_id=t.company_id AND next_ticket.customer_id=t.customer_id
            AND next_ticket.ticket_id<>t.ticket_id
        WHERE n.standardized_at>=t.tracked_at
          AND coalesce(n.occurred_at,n.received_at)>coalesce(root.occurred_at,root.received_at)
          -- The closed Gmail thread rule is pending team agreement.
          AND (root.source_channel_type<>'gmail' OR t.status<>'답변완료')
    """
    if summary:
        query = "SELECT count(*) AS count, count(DISTINCT ticket_id) AS ticket_count FROM (" + query + ") events"
    else:
        query += " ORDER BY occurred_at DESC, event_uuid, ticket_id"
    cursor.execute(query, (company, staff))
    rows = fetch_rows(cursor)
    return rows[0] if summary else rows


def source_exists(cursor, company, ticket, chat):
    if ticket:
        cursor.execute("SELECT 1 FROM visible_ticket WHERE company_id=%s AND ticket_id=%s", (company, ticket))
    else:
        cursor.execute("SELECT 1 FROM chat WHERE company_id=%s AND chat_id=%s", (company, chat))
    return cursor.fetchone() is not None


def track_options(cursor, company, staff):
    cursor.execute("""
        SELECT t.ticket_id, t.ticket_number, t.title
        FROM visible_ticket t
        WHERE t.company_id=%s AND NOT EXISTS (
            SELECT FROM followup_track f WHERE f.company_id=t.company_id
              AND f.ticket_id=t.ticket_id AND f.staff_user_id=%s
        )
        ORDER BY t.created_at DESC, t.ticket_id
    """, (company, staff))
    return fetch_rows(cursor)


def add_contact(cursor, company, staff, data, *, reminders=False):
    cursor.execute("""
        INSERT INTO followup_contact(company_id,created_by_id,ticket_id,chat_id,assignee_id,reason,scheduled_at,
                                     reminders_started_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,CASE WHEN %s THEN clock_timestamp() END) RETURNING contact_id
    """, (company, staff, data["ticket_id"], data["chat_id"], data["assignee_id"],
          data["reason"], data["scheduled_at"], reminders))
    return cursor.fetchone()[0]


def contacts(cursor, company, staff, can_read, can_chat, *, ident=None, filters=None, lock=False):
    query = """
        SELECT c.contact_id, c.ticket_id, c.chat_id, t.ticket_number,
               coalesce(t.title, left(first_message.body, 100)) AS title,
               c.reminders_started_at, c.reminders_stopped_at,
               c.reason, c.scheduled_at, c.assignee_id, coalesce(a.name,a.email) AS assignee,
               c.contacted_at, c.completed_by_id, coalesce(p.name,p.email) AS completed_by, c.result
        FROM followup_contact c
        LEFT JOIN visible_ticket t USING (company_id,ticket_id)
        LEFT JOIN LATERAL (
            SELECT body FROM chat_message WHERE chat_id=c.chat_id AND sender_type='customer'
            ORDER BY sent_at, chat_message_id LIMIT 1
        ) first_message ON true
        JOIN staff_user a ON a.staff_user_id=c.assignee_id
        LEFT JOIN staff_user p ON p.staff_user_id=c.completed_by_id
        WHERE c.company_id=%s AND %s IN (c.created_by_id,c.assignee_id)
          AND ((%s AND t.ticket_id IS NOT NULL) OR (%s AND c.chat_id IS NOT NULL))
    """
    params = [company, staff, can_read, can_chat]
    if ident is not None:
        query += " AND c.contact_id=%s"
        params.append(ident)
        if lock:
            query += " FOR UPDATE OF c"
    elif filters is not None:
        query += " AND (c.contacted_at IS NOT NULL)=%s"
        params.append(filters["state"] == "completed")
        if filters["source"]:
            query += " AND (c.ticket_id IS NOT NULL)=%s"
            params.append(filters["source"] == "ticket")
        if filters["q"]:
            query += """
                AND strpos(lower(concat_ws(' ', '#' || t.ticket_number, c.chat_id, t.title, first_message.body,
                                           c.reason, coalesce(a.name,a.email), coalesce(p.name,p.email), c.result)),
                           lower(%s)) > 0
            """
            params.append(filters["q"])
        query += " ORDER BY coalesce(c.contacted_at,c.scheduled_at) DESC, c.contact_id"
    cursor.execute(query, params)
    return fetch_rows(cursor)


def complete_contact(cursor, ident, staff, contacted_at, result):
    cursor.execute("""
        UPDATE followup_contact SET completed_by_id=%s, contacted_at=%s, result=%s,
            reminders_stopped_at=CASE WHEN reminders_started_at IS NOT NULL
                THEN coalesce(reminders_stopped_at,clock_timestamp()) END
        WHERE contact_id=%s
    """, (staff, contacted_at, result, ident))


def chat_detail(cursor, company, chat):
    cursor.execute("""
        SELECT chat_id, customer_id AS customer_uuid, status, waiting_at, started_at, ended_at, assignee_id
        FROM chat WHERE company_id=%s AND chat_id=%s
    """, (company, chat))
    item = next(iter(fetch_rows(cursor)), None)
    if item:
        cursor.execute("""
            SELECT chat_message_id, sender_type, message_type, body, sent_at FROM chat_message
            WHERE chat_id=%s ORDER BY sent_at, chat_message_id
        """, (chat,))
        item["messages"] = fetch_rows(cursor)
    return item

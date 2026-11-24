from config.database import fetch_rows


REQUEST_FROM = """
    FROM collab_request r
    JOIN visible_ticket t ON t.ticket_uuid=r.ticket_uuid
    JOIN staff_user requester ON requester.staff_user_uuid=r.requester_uuid AND requester.company_uuid=t.company_uuid
    JOIN staff_user recipient ON recipient.staff_user_uuid=r.recipient_uuid AND recipient.company_uuid=t.company_uuid
"""
REPLY_JOIN = """
    JOIN collab_review_reply rep ON rep.collab_request_uuid=r.collab_request_uuid AND rep.author_uuid=r.recipient_uuid
"""
LEGACY_FROM = """
    FROM ticket_event e JOIN event_type et USING(event_type_uuid)
    JOIN visible_ticket t USING(ticket_uuid)
    JOIN staff_user recipient ON recipient.staff_user_uuid=e.recipient_uuid AND recipient.company_uuid=t.company_uuid
    LEFT JOIN staff_user requester ON requester.staff_user_uuid=e.actor_uuid AND requester.company_uuid=t.company_uuid
    WHERE t.company_uuid=%s AND e.recipient_uuid=%s AND et.code='ticket_transfer'
      AND e.collab_request_uuid IS NULL AND e.transfer_reason IS NOT NULL
      AND (e.actor_uuid IS NULL OR requester.staff_user_uuid IS NOT NULL)
"""
TICKET_COLUMNS = """
    t.ticket_uuid, t.ticket_number, t.title, t.status AS ticket_status, t.created_at AS ticket_received_at,
    t.sentiment, t.sentiment_status, t.category_status,
    (SELECT name FROM category WHERE category_uuid=t.category_uuid AND domain_uuid=t.domain_uuid) AS category
"""


def received(cursor, scope):
    cursor.execute(f"""
        SELECT {TICKET_COLUMNS}, r.collab_request_uuid AS id, r.request_type AS kind,
               r.status AS request_status, r.created_at AS received_at, r.read_at, r.memo,
               coalesce(requester.name, requester.email) AS sender,
               ARRAY(SELECT a.original_filename FROM collab_request_attachment a
                     WHERE a.collab_request_uuid=r.collab_request_uuid
                     ORDER BY a.created_at, a.collab_request_attachment_uuid) AS attachment_names,
               rep.body AS sent_reply, NULL AS original_memo
        {REQUEST_FROM}
        LEFT JOIN collab_review_reply rep ON r.request_type='review' AND rep.collab_request_uuid=r.collab_request_uuid
            AND rep.author_uuid=r.recipient_uuid
        WHERE t.company_uuid=%s AND r.recipient_uuid=%s
        UNION ALL
        SELECT {TICKET_COLUMNS}, e.ticket_event_uuid, 'transfer', 'accepted', e.created_at, e.created_at, e.transfer_reason,
               coalesce(requester.name, requester.email), ARRAY[]::text[], NULL, NULL
        {LEGACY_FROM}
        UNION ALL
        SELECT {TICKET_COLUMNS}, rep.collab_review_reply_uuid, 'reply', NULL, rep.created_at, rep.read_at, rep.body,
               coalesce(recipient.name, recipient.email), ARRAY[]::text[], NULL, r.memo
        {REQUEST_FROM} {REPLY_JOIN}
        WHERE t.company_uuid=%s AND r.requester_uuid=%s AND r.request_type='review'
        ORDER BY received_at DESC, id
    """, scope * 3)
    return fetch_rows(cursor)


def transfer_history(cursor, company, ticket):
    cursor.execute("""
        SELECT coalesce(recipient.name, recipient.email) AS recipient,
               e.created_at, coalesce(r.memo,e.transfer_reason) AS memo
        FROM ticket_event e JOIN event_type et USING(event_type_uuid)
        JOIN visible_ticket t USING(ticket_uuid)
        JOIN staff_user recipient ON recipient.staff_user_uuid=e.recipient_uuid AND recipient.company_uuid=t.company_uuid
        LEFT JOIN staff_user actor ON actor.staff_user_uuid=e.actor_uuid AND actor.company_uuid=t.company_uuid
        LEFT JOIN collab_request r ON r.collab_request_uuid=e.collab_request_uuid AND r.ticket_uuid=e.ticket_uuid
            AND r.recipient_uuid=e.recipient_uuid AND r.request_type='transfer'
        LEFT JOIN staff_user requester ON requester.staff_user_uuid=r.requester_uuid AND requester.company_uuid=t.company_uuid
        WHERE t.company_uuid=%s AND t.ticket_uuid=%s AND et.code='ticket_transfer'
            AND (e.actor_uuid IS NULL OR actor.staff_user_uuid IS NOT NULL)
            AND (e.collab_request_uuid IS NULL OR requester.staff_user_uuid IS NOT NULL)
        ORDER BY e.created_at DESC, e.ticket_event_uuid
    """, (company, ticket))
    return fetch_rows(cursor)


def mark_request_read(cursor, scope, request_id):
    cursor.execute(f"""
        UPDATE collab_request SET read_at=coalesce(read_at, now())
        WHERE collab_request_uuid=(
            SELECT r.collab_request_uuid {REQUEST_FROM}
            WHERE t.company_uuid=%s AND r.recipient_uuid=%s AND r.collab_request_uuid=%s
        ) RETURNING read_at
    """, (*scope, request_id))
    return cursor.fetchone()


def mark_reply_read(cursor, scope, reply_id):
    cursor.execute(f"""
        UPDATE collab_review_reply SET read_at=coalesce(read_at, now())
        WHERE collab_review_reply_uuid=(
            SELECT rep.collab_review_reply_uuid {REQUEST_FROM} {REPLY_JOIN}
            WHERE t.company_uuid=%s AND r.requester_uuid=%s AND r.request_type='review'
                AND rep.collab_review_reply_uuid=%s
        ) RETURNING read_at
    """, (*scope, reply_id))
    return cursor.fetchone()


def lock_review(cursor, scope, request_id):
    cursor.execute(f"""
        SELECT r.status {REQUEST_FROM}
        WHERE t.company_uuid=%s AND r.recipient_uuid=%s AND r.collab_request_uuid=%s AND r.request_type='review'
        FOR UPDATE OF r
    """, (*scope, request_id))
    review = cursor.fetchone()
    if review is None:
        return None
    # A waiter may have taken its initial snapshot before the winning reply committed.
    cursor.execute("SELECT body FROM collab_review_reply WHERE collab_request_uuid=%s", (request_id,))
    return review[0], (cursor.fetchone() or (None,))[0]


def insert_reply(cursor, request_id, author, body):
    cursor.execute("INSERT INTO collab_review_reply(collab_request_uuid,author_uuid,body) VALUES (%s,%s,%s)",
                   (request_id, author, body))
    cursor.execute("""
        UPDATE collab_request SET status='replied', read_at=coalesce(read_at, now()) WHERE collab_request_uuid=%s
    """, (request_id,))

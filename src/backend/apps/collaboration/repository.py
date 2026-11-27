from config.database import fetch_rows


REQUEST_FROM = """
    FROM collab_request r
    JOIN visible_ticket t ON t.ticket_id=r.ticket_id
    JOIN staff_user requester ON requester.staff_user_id=r.requester_id AND requester.company_id=t.company_id
    JOIN staff_user recipient ON recipient.staff_user_id=r.recipient_id AND recipient.company_id=t.company_id
"""
REPLY_JOIN = """
    JOIN collab_review_reply rep ON rep.collab_request_id=r.collab_request_id AND rep.author_id=r.recipient_id
"""
LEGACY_FROM = """
    FROM ticket_event e JOIN event_type et USING(event_type_id)
    JOIN visible_ticket t USING(ticket_id)
    JOIN staff_user recipient ON recipient.staff_user_id=e.recipient_id AND recipient.company_id=t.company_id
    LEFT JOIN staff_user requester ON requester.staff_user_id=e.actor_id AND requester.company_id=t.company_id
    WHERE t.company_id=%s AND e.recipient_id=%s AND et.code='ticket_transfer'
      AND e.collab_request_id IS NULL AND e.transfer_reason IS NOT NULL
      AND (e.actor_id IS NULL OR requester.staff_user_id IS NOT NULL)
"""
TICKET_COLUMNS = """
    t.ticket_id, t.ticket_number, t.title, t.status AS ticket_status, t.created_at AS ticket_received_at,
    t.sentiment, t.sentiment_status, t.category_status,
    (SELECT name FROM category WHERE category_id=t.category_id AND domain_id=t.domain_id) AS category
"""


def download_options(cursor, scope, request):
    cursor.execute(f"""
        SELECT a.collab_request_attachment_id AS id, a.original_filename AS filename
        {REQUEST_FROM} JOIN collab_request_attachment a USING (collab_request_id)
        WHERE t.company_id=%s AND r.recipient_id=%s AND r.collab_request_id=%s
          AND a.content IS NOT NULL
        ORDER BY a.created_at, a.collab_request_attachment_id
    """, (*scope, request))
    return fetch_rows(cursor)


def received(cursor, scope):
    cursor.execute(f"""
        SELECT {TICKET_COLUMNS}, r.collab_request_id AS id, r.request_type AS kind,
               r.status AS request_status, r.created_at AS received_at, r.read_at, r.memo,
               coalesce(requester.name, requester.email) AS sender,
               ARRAY(SELECT a.original_filename FROM collab_request_attachment a
                     WHERE a.collab_request_id=r.collab_request_id
                     ORDER BY a.created_at, a.collab_request_attachment_id) AS attachment_names,
               rep.body AS sent_reply, NULL AS original_memo,
               EXISTS (SELECT FROM collab_request_attachment a WHERE a.collab_request_id=r.collab_request_id
                       AND a.content IS NOT NULL) AS has_download
        {REQUEST_FROM}
        LEFT JOIN collab_review_reply rep ON r.request_type='review' AND rep.collab_request_id=r.collab_request_id
            AND rep.author_id=r.recipient_id
        WHERE t.company_id=%s AND r.recipient_id=%s
        UNION ALL
        SELECT {TICKET_COLUMNS}, e.ticket_event_id, 'transfer', 'accepted', e.created_at, e.created_at, e.transfer_reason,
               coalesce(requester.name, requester.email), ARRAY[]::text[], NULL, NULL, false
        {LEGACY_FROM}
        ORDER BY received_at DESC, id
    """, scope * 2)
    return fetch_rows(cursor)


def sent(cursor, scope):
    cursor.execute(f"""
        SELECT {TICKET_COLUMNS}, r.collab_request_id AS id, r.request_type AS kind,
               r.status AS request_status, r.created_at AS received_at, r.memo,
               coalesce(recipient.name, recipient.email) AS counterpart,
               ARRAY(SELECT a.original_filename FROM collab_request_attachment a
                     WHERE a.collab_request_id=r.collab_request_id
                     ORDER BY a.created_at, a.collab_request_attachment_id) AS attachment_names,
               rep.body AS sent_reply, rep.collab_review_reply_id AS reply_id, rep.read_at
        {REQUEST_FROM}
        LEFT JOIN collab_review_reply rep ON r.request_type='review' AND rep.collab_request_id=r.collab_request_id
            AND rep.author_id=r.recipient_id
        WHERE t.company_id=%s AND r.requester_id=%s
        ORDER BY r.created_at DESC, r.collab_request_id
    """, scope)
    return fetch_rows(cursor)


def transfer_history(cursor, company, ticket):
    cursor.execute("""
        SELECT coalesce(recipient.name, recipient.email) AS recipient,
               e.created_at, coalesce(r.memo,e.transfer_reason) AS memo
        FROM ticket_event e JOIN event_type et USING(event_type_id)
        JOIN visible_ticket t USING(ticket_id)
        JOIN staff_user recipient ON recipient.staff_user_id=e.recipient_id AND recipient.company_id=t.company_id
        LEFT JOIN staff_user actor ON actor.staff_user_id=e.actor_id AND actor.company_id=t.company_id
        LEFT JOIN collab_request r ON r.collab_request_id=e.collab_request_id AND r.ticket_id=e.ticket_id
            AND r.recipient_id=e.recipient_id AND r.request_type='transfer'
        LEFT JOIN staff_user requester ON requester.staff_user_id=r.requester_id AND requester.company_id=t.company_id
        WHERE t.company_id=%s AND t.ticket_id=%s AND et.code='ticket_transfer'
            AND (e.actor_id IS NULL OR actor.staff_user_id IS NOT NULL)
            AND (e.collab_request_id IS NULL OR requester.staff_user_id IS NOT NULL)
        ORDER BY e.created_at DESC, e.ticket_event_id
    """, (company, ticket))
    return fetch_rows(cursor)


def mark_request_read(cursor, scope, request_id):
    cursor.execute(f"""
        UPDATE collab_request SET read_at=coalesce(read_at, now())
        WHERE collab_request_id=(
            SELECT r.collab_request_id {REQUEST_FROM}
            WHERE t.company_id=%s AND r.recipient_id=%s AND r.collab_request_id=%s
        ) RETURNING read_at
    """, (*scope, request_id))
    return cursor.fetchone()


def mark_reply_read(cursor, scope, reply_id):
    cursor.execute(f"""
        UPDATE collab_review_reply SET read_at=coalesce(read_at, now())
        WHERE collab_review_reply_id=(
            SELECT rep.collab_review_reply_id {REQUEST_FROM} {REPLY_JOIN}
            WHERE t.company_id=%s AND r.requester_id=%s AND r.request_type='review'
                AND rep.collab_review_reply_id=%s
        ) RETURNING read_at
    """, (*scope, reply_id))
    return cursor.fetchone()


def lock_review(cursor, scope, request_id):
    cursor.execute(f"""
        SELECT r.status {REQUEST_FROM}
        WHERE t.company_id=%s AND r.recipient_id=%s AND r.collab_request_id=%s AND r.request_type='review'
        FOR UPDATE OF r
    """, (*scope, request_id))
    review = cursor.fetchone()
    if review is None:
        return None
    # A waiter may have taken its initial snapshot before the winning reply committed.
    cursor.execute("SELECT body FROM collab_review_reply WHERE collab_request_id=%s", (request_id,))
    return review[0], (cursor.fetchone() or (None,))[0]


def insert_reply(cursor, request_id, author, body):
    cursor.execute("INSERT INTO collab_review_reply(collab_request_id,author_id,body) VALUES (%s,%s,%s)",
                   (request_id, author, body))
    cursor.execute("""
        UPDATE collab_request SET status='replied', read_at=coalesce(read_at, now()) WHERE collab_request_id=%s
    """, (request_id,))

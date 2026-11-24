from config.database import fetch_rows

SORTS = {
    "default": "t.created_at ASC",
    "received-asc": "t.created_at ASC",
    "received-desc": "t.created_at DESC",
    "updated-asc": "t.updated_at ASC",
    "updated-desc": "t.updated_at DESC",
}
TICKET_COLUMNS = """
    t.ticket_uuid, t.ticket_number, c.external_customer_id AS customer_id, t.title,
    tm.name AS team_name, COALESCE(st.name, st.email) AS assignee_name, t.status,
    t.category_uuid, cat.name AS category, t.category_status, t.sentiment, t.sentiment_status,
    (t.classification_final_at IS NOT NULL AND
      (t.sentiment_status='failed' OR t.category_status='failed')) AS classification_failed,
    t.created_at, t.updated_at, ch.type AS channel
"""
TICKET_FROM = """
    FROM visible_ticket AS t
    JOIN customer AS c ON c.customer_uuid=t.customer_uuid AND c.company_uuid=t.company_uuid
    JOIN channel AS ch ON ch.channel_uuid=t.channel_uuid AND ch.company_uuid=t.company_uuid
    LEFT JOIN team AS tm ON tm.team_uuid=t.assigned_team_uuid AND tm.company_uuid=t.company_uuid
    LEFT JOIN staff_user AS st ON st.staff_user_uuid=t.assignee_uuid AND st.company_uuid=t.company_uuid
    LEFT JOIN category AS cat ON cat.category_uuid=t.category_uuid AND cat.domain_uuid=t.domain_uuid
"""


def filter_options(cursor, company_uuid):
    company = (company_uuid,)
    cursor.execute("SELECT team_uuid AS id,name FROM team WHERE company_uuid=%s ORDER BY name,id", company)
    teams = fetch_rows(cursor)
    cursor.execute("""SELECT staff_user_uuid AS id,coalesce(name,email) AS name
                      FROM staff_user WHERE company_uuid=%s ORDER BY name,id""", company)
    assignees = fetch_rows(cursor)
    cursor.execute("""SELECT category_uuid AS id,cat.name FROM category cat
                      JOIN company c ON c.domain_uuid=cat.domain_uuid
                      WHERE c.company_uuid=%s ORDER BY cat.name,id""", company)
    return {"teams": teams, "assignees": assignees, "categories": fetch_rows(cursor)}


def detail(cursor, company_uuid, ticket_uuid):
    cursor.execute("SELECT " + TICKET_COLUMNS + ", t.customer_uuid " + TICKET_FROM
                   + " WHERE t.ticket_uuid=%s AND t.company_uuid=%s", (str(ticket_uuid), company_uuid))
    ticket = next(iter(fetch_rows(cursor)), None)
    if ticket is None:
        return None
    cursor.execute("""
        SELECT t.ticket_uuid,t.ticket_number,m.sender_type,m.message_type,m.body,m.created_at
        FROM visible_ticket t JOIN ticket_message m ON m.ticket_uuid=t.ticket_uuid
        WHERE t.company_uuid=%s AND t.customer_uuid=%s
        ORDER BY m.created_at,t.ticket_number,m.ticket_message_uuid
        """, (company_uuid, ticket.pop("customer_uuid")))
    messages = fetch_rows(cursor)
    cursor.execute("""
        SELECT peer.ticket_uuid,peer.ticket_number,peer.title,ch.type AS channel
        FROM ticket_duplicate_candidate link
        JOIN visible_ticket peer ON peer.ticket_uuid =
            CASE WHEN link.ticket_uuid=%s THEN link.candidate_ticket_uuid ELSE link.ticket_uuid END
        JOIN channel ch ON ch.channel_uuid=peer.channel_uuid AND ch.company_uuid=peer.company_uuid
        WHERE (link.ticket_uuid=%s OR link.candidate_ticket_uuid=%s)
            AND peer.company_uuid=%s AND ch.type<>%s
        ORDER BY peer.created_at,peer.ticket_number
        """, (str(ticket_uuid), str(ticket_uuid), str(ticket_uuid), company_uuid, ticket["channel"]))
    return {"ticket": ticket, "messages": messages, "duplicate_candidates": fetch_rows(cursor)}


def _id_filter(conditions, params, column, entries):
    if entries:
        ids = [entry for entry in entries if entry != "unassigned"]
        clauses = []
        if ids:
            clauses.append(column + " = ANY(%s::uuid[])")
            params.append(ids)
        if "unassigned" in entries:
            clauses.append(column + " IS NULL")
        conditions.append("(" + " OR ".join(clauses) + ")")


def list_tickets(cursor, company_uuid, assignee_uuid, query):
    conditions = ["t.company_uuid=%s"]
    params = [company_uuid]
    if assignee_uuid:
        conditions.append("t.assignee_uuid=%s")
        params.append(assignee_uuid)
    if query.q:
        search = {"title": "t.title", "customer": "c.external_customer_id", "id": "('#' || t.ticket_number)"}
        conditions.append(f"strpos(lower({search[query.search_by]}), lower(%s)) > 0")
        params.append(query.q)
    for key, column in (("team", "t.assigned_team_uuid"), ("assignee", "t.assignee_uuid"),
                        ("category", "t.category_uuid")):
        _id_filter(conditions, params, column, query.filters[key])
    for key in ("status", "sentiment"):
        if query.filters[key]:
            conditions.append("t." + key + " = ANY(%s)")
            params.append(query.filters[key])
    if query.days is not None:
        today = "(now() AT TIME ZONE 'Asia/Seoul')::date"
        conditions.extend((f"t.created_at >= (({today} - %s)::timestamp AT TIME ZONE 'Asia/Seoul')",
                           f"t.created_at < (({today} + 1)::timestamp AT TIME ZONE 'Asia/Seoul')"))
        params.append(query.days - 1)
    if query.start is not None:
        conditions.extend(("t.created_at >= (%s::date::timestamp AT TIME ZONE 'Asia/Seoul')",
                           "t.created_at < ((%s::date + 1)::timestamp AT TIME ZONE 'Asia/Seoul')"))
        params.extend((query.start, query.end))
    filtered = TICKET_FROM + " WHERE " + " AND ".join(conditions)
    cursor.execute("SELECT count(*) " + filtered, params)
    total = cursor.fetchone()[0]
    page = min(query.page, max(1, (total + query.page_size - 1) // query.page_size))
    cursor.execute("SELECT " + TICKET_COLUMNS + filtered + " ORDER BY " + SORTS[query.sort]
                   + ", t.ticket_number ASC LIMIT %s OFFSET %s",
                   [*params, query.page_size, (page - 1) * query.page_size])
    return {"items": fetch_rows(cursor), "total": total, "page": page, "page_size": query.page_size}


def add_memo(cursor, company, staff, ticket, body):
    cursor.execute("""
        INSERT INTO ticket_message(ticket_uuid,staff_user_uuid,body,sender_type,message_type,created_at)
        SELECT ticket_uuid,%s,%s,'staff','memo',clock_timestamp()
        FROM visible_ticket WHERE company_uuid=%s AND ticket_uuid=%s
        RETURNING ticket_message_uuid, ticket_uuid, staff_user_uuid, body, created_at
    """, (staff, body, company, ticket))
    return next(iter(fetch_rows(cursor)), None)


def displayed_predictions(cursor, ticket_ids):
    cursor.execute("""
        SELECT p.ticket_uuid::text,p.kind,p.value,p.attempt_id::text
        FROM voc.ticket_prediction p JOIN visible_ticket t USING(ticket_uuid)
        WHERE p.ticket_uuid=ANY(%s::uuid[]) AND (
            (p.kind='sentiment' AND p.value=t.sentiment) OR
            (p.kind='category' AND p.value=t.category_uuid::text))
    """, ([str(ticket) for ticket in ticket_ids],))
    return fetch_rows(cursor)


def record_display(cursor, actor, prediction):
    cursor.execute("""
        INSERT INTO voc.prediction_display(ticket_uuid,kind,staff_user_uuid,displayed_value,attempt_id)
        SELECT p.ticket_uuid,p.kind,%s,p.value,p.attempt_id
        FROM voc.ticket_prediction p JOIN public.ticket t USING(ticket_uuid)
        WHERE p.ticket_uuid=%s AND p.kind=%s AND p.value=%s AND p.attempt_id=%s
          AND t.company_uuid=%s
        ON CONFLICT DO NOTHING
    """, (actor["staff_user_uuid"], prediction["ticket_uuid"], prediction["kind"], prediction["value"],
          prediction["attempt_id"], actor["company_uuid"]))


def can_reply(cursor, actor):
    cursor.execute("""
        SELECT EXISTS (SELECT FROM staff_permission JOIN permission USING(permission_uuid)
                       WHERE staff_user_uuid=%s AND code='ticket.reply')
    """, (actor["staff_user_uuid"],))
    return cursor.fetchone()[0]


def confirmed_replies(cursor, actor, ticket):
    cursor.execute("""
        SELECT ticket_message_uuid,created_at FROM ticket_message
        WHERE ticket_uuid=%s AND staff_user_uuid=%s AND sender_type='staff'
          AND message_type='reply' AND delivery_status='delivered'
        ORDER BY created_at DESC LIMIT 1
    """, (ticket, actor["staff_user_uuid"]))
    return fetch_rows(cursor)


def lock_visible(cursor, actor, ticket):
    cursor.execute("""
        SELECT t.domain_uuid,t.status,t.reply_delivery_uuid FROM public.ticket t
        JOIN visible_ticket v USING(ticket_uuid)
        WHERE t.ticket_uuid=%s AND t.company_uuid=%s FOR UPDATE OF t
    """, (ticket, actor["company_uuid"]))
    return next(iter(fetch_rows(cursor)), None)


def set_classification(cursor, ticket, domain, sentiment, category):
    if category:
        cursor.execute("SELECT 1 FROM category WHERE domain_uuid=%s AND category_uuid=%s", (domain, category))
        if cursor.fetchone() is None:
            return False
    cursor.execute("""
        UPDATE ticket SET sentiment=%s,category_uuid=%s,updated_at=clock_timestamp() WHERE ticket_uuid=%s
    """, (sentiment, category, ticket))
    return True


def complete_delivered(cursor, actor, ticket, delivery):
    cursor.execute("""
        UPDATE ticket t SET status='답변완료',reply_delivery_uuid=m.ticket_message_uuid,updated_at=clock_timestamp()
        FROM ticket_message m
        WHERE t.ticket_uuid=%s AND t.status<>'답변완료' AND m.ticket_uuid=t.ticket_uuid AND m.ticket_message_uuid=%s
          AND m.staff_user_uuid=%s AND m.sender_type='staff' AND m.message_type='reply' AND m.delivery_status='delivered'
    """, (ticket, delivery, actor["staff_user_uuid"]))
    return cursor.rowcount == 1

from config.database import fetch_rows

from .status import CLOSE_AT_SQL, STATUS_SQL

SORTS = {
    "default": "t.created_at ASC",
    "received-asc": "t.created_at ASC",
    "received-desc": "t.created_at DESC",
    "updated-asc": "t.updated_at ASC",
    "updated-desc": "t.updated_at DESC",
}
TICKET_COLUMNS = f"""
    t.ticket_id, t.ticket_number, c.external_customer_id AS customer_id, t.title,
    tm.name AS team_name, COALESCE(st.name, st.email) AS assignee_name, {STATUS_SQL} AS status,
    cat.name AS category, t.category_status, t.category_review_reason, t.sentiment, t.sentiment_status,
    t.created_at, t.updated_at, ch.type AS channel
"""
TICKET_FROM = """
    FROM visible_ticket AS t
    JOIN customer AS c ON c.customer_id=t.customer_id AND c.company_id=t.company_id
    JOIN channel AS ch ON ch.channel_id=t.channel_id AND ch.company_id=t.company_id
    LEFT JOIN team AS tm ON tm.team_id=t.assigned_team_id AND tm.company_id=t.company_id
    LEFT JOIN staff_user AS st ON st.staff_user_id=t.assignee_id AND st.company_id=t.company_id
    LEFT JOIN category AS cat ON cat.category_id=t.category_id AND cat.domain_id=t.domain_id
"""


def filter_options(cursor, company_id):
    company = (company_id,)
    cursor.execute("SELECT team_id AS id,name FROM team WHERE company_id=%s ORDER BY name,id", company)
    teams = fetch_rows(cursor)
    cursor.execute("""SELECT staff_user_id AS id,coalesce(name,email) AS name
                      FROM staff_user WHERE company_id=%s ORDER BY name,id""", company)
    assignees = fetch_rows(cursor)
    cursor.execute("""SELECT category_id AS id,cat.name FROM category cat
                      JOIN company c ON c.domain_id=cat.domain_id
                      WHERE c.company_id=%s ORDER BY cat.name,id""", company)
    return {"teams": teams, "assignees": assignees, "categories": fetch_rows(cursor)}


def detail(cursor, company_id, ticket_id, include_memos=False, *, for_page=False):
    customer_fields = f", c.name AS customer_name, c.email AS customer_email, ({CLOSE_AT_SQL}) AS close_at" if for_page else ""
    cursor.execute("SELECT " + TICKET_COLUMNS + ", t.customer_id AS internal_customer_id,t.assignee_id,t.csv_import_kind"
                   + customer_fields + TICKET_FROM
                   + " WHERE t.ticket_id=%s AND t.company_id=%s", (str(ticket_id), company_id))
    ticket = next(iter(fetch_rows(cursor)), None)
    if ticket is None:
        return None
    customer_id = ticket.pop("internal_customer_id")
    cursor.execute(f"""
        SELECT t.ticket_id,t.ticket_number,m.sender_type,m.message_type,m.body,m.created_at,m.delivery_status,
               {"m.staff_user_id,m.ticket_message_id," if for_page else ""}
               CASE WHEN m.sender_type='customer' THEN c.name ELSE s.name END AS author_name
        FROM visible_ticket t JOIN ticket_message m ON m.ticket_id=t.ticket_id
        JOIN customer c ON c.customer_id=t.customer_id AND c.company_id=t.company_id
        LEFT JOIN staff_user s ON s.staff_user_id=m.staff_user_id AND s.company_id=t.company_id
        WHERE t.company_id=%s AND t.ticket_id=%s AND (%s OR m.message_type<>'memo')
        ORDER BY m.created_at,m.ticket_message_id
        """, (company_id, str(ticket_id), include_memos))
    messages, memos = [], []
    for message in fetch_rows(cursor):
        (memos if message["message_type"] == "memo" else messages).append(message)
    cursor.execute("SELECT " + TICKET_COLUMNS + TICKET_FROM + """
        WHERE t.company_id=%s AND t.customer_id=%s AND t.ticket_id<>%s
        ORDER BY t.created_at,t.ticket_number
    """, (company_id, customer_id, str(ticket_id)))
    related = fetch_rows(cursor)
    cursor.execute("""
        SELECT peer.ticket_id,peer.ticket_number,peer.title,ch.type AS channel
        FROM ticket_duplicate_candidate link
        JOIN visible_ticket peer ON peer.ticket_id =
            CASE WHEN link.ticket_id=%s THEN link.candidate_ticket_id ELSE link.ticket_id END
        JOIN channel ch ON ch.channel_id=peer.channel_id AND ch.company_id=peer.company_id
        WHERE (link.ticket_id=%s OR link.candidate_ticket_id=%s)
            AND peer.company_id=%s AND ch.type<>%s
        ORDER BY peer.created_at,peer.ticket_number
        """, (str(ticket_id), str(ticket_id), str(ticket_id), company_id, ticket["channel"]))
    result = {"ticket": ticket, "messages": messages, "memos": memos, "customer_tickets": related,
              "duplicate_candidates": fetch_rows(cursor)}
    if for_page:
        visible_messages = messages + memos
        cursor.execute("""
            SELECT ticket_message_id, attachment_id, filename
            FROM ticket_message_attachment WHERE ticket_message_id = ANY(%s::uuid[])
            ORDER BY attachment_id
        """, ([row["ticket_message_id"] for row in visible_messages],))
        attachments = {}
        for row in fetch_rows(cursor):
            attachments.setdefault(row["ticket_message_id"], []).append(row)
        for row in visible_messages:
            row["attachments"] = attachments.get(row["ticket_message_id"], ())
    if for_page and include_memos:
        cursor.execute("""
            SELECT 'staff' AS sender_type, 'transfer' AS message_type, e.transfer_reason AS body, e.created_at,
                   coalesce(st.name, st.email) AS author_name
            FROM visible_ticket t
            JOIN ticket_event e USING (ticket_id)
            JOIN event_type et USING (event_type_id)
            LEFT JOIN staff_user st ON st.staff_user_id=e.actor_id AND st.company_id=t.company_id
            WHERE t.ticket_id=%s AND t.company_id=%s AND et.code='ticket_transfer'
              AND e.transfer_reason IS NOT NULL
        """, (str(ticket_id), company_id))
        result["transfer_notes"] = fetch_rows(cursor)
    return result


def summary_source(cursor, company_id, ticket_id):
    cursor.execute(f"""
        SELECT {STATUS_SQL} AS status, ch.type AS channel, COALESCE(
            (SELECT h.model_text FROM voc.model_text_history h
             WHERE h.company_id=t.company_id AND h.event_id=t.source_event_id
             ORDER BY h.body_version DESC, h.preprocess_version DESC LIMIT 1),
            (SELECT string_agg(m.body, E'\\n' ORDER BY m.created_at,m.ticket_message_id)
             FROM ticket_message m WHERE m.ticket_id=t.ticket_id
               AND m.sender_type='customer' AND m.message_type='inquiry')
        ) AS body
        FROM visible_ticket t JOIN channel ch ON ch.channel_id=t.channel_id AND ch.company_id=t.company_id
        WHERE t.company_id=%s AND t.ticket_id=%s
    """, (company_id, ticket_id))
    return next(iter(fetch_rows(cursor)), None)


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


def list_tickets(cursor, company_id, assignee_id, query):
    conditions = ["t.company_id=%s"]
    params = [company_id]
    if assignee_id:
        conditions.append("t.assignee_id=%s")
        params.append(assignee_id)
    if query.q:
        search = {"title": "t.title", "customer": "c.external_customer_id", "id": "('#' || t.ticket_number)"}
        conditions.append(f"strpos(lower({search[query.search_by]}), lower(%s)) > 0")
        params.append(query.q)
    for key, column in (("team", "t.assigned_team_id"), ("assignee", "t.assignee_id"),
                        ("category", "t.category_id")):
        _id_filter(conditions, params, column, query.filters[key])
    for key in ("status", "sentiment"):
        if query.filters[key]:
            column = f"({STATUS_SQL})" if key == "status" else "t.sentiment"
            conditions.append(column + " = ANY(%s)")
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
        INSERT INTO ticket_message(ticket_id,staff_user_id,body,sender_type,message_type,created_at)
        SELECT ticket_id,%s,%s,'staff','memo',clock_timestamp()
        FROM visible_ticket WHERE company_id=%s AND ticket_id=%s
        RETURNING ticket_message_id, ticket_id, staff_user_id, body, created_at
    """, (staff, body, company, ticket))
    return next(iter(fetch_rows(cursor)), None)

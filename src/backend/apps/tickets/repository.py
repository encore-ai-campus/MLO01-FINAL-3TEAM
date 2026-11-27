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
    cat.name AS category, t.category_status, t.category_review_reason, t.sentiment, t.sentiment_status,
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


def detail(cursor, company_uuid, ticket_uuid, include_memos=False, *, for_page=False):
    customer_fields = ", c.name AS customer_name, c.email AS customer_email" if for_page else ""
    cursor.execute("SELECT " + TICKET_COLUMNS + ", t.customer_uuid,t.assignee_uuid,t.csv_import_kind"
                   + customer_fields + TICKET_FROM
                   + " WHERE t.ticket_uuid=%s AND t.company_uuid=%s", (str(ticket_uuid), company_uuid))
    ticket = next(iter(fetch_rows(cursor)), None)
    if ticket is None:
        return None
    customer_uuid = ticket.pop("customer_uuid")
    cursor.execute(f"""
        SELECT t.ticket_uuid,t.ticket_number,m.sender_type,m.message_type,m.body,m.created_at,m.delivery_status,
               {"m.staff_user_uuid," if for_page else ""}
               CASE WHEN m.sender_type='customer' THEN c.name ELSE s.name END AS author_name
        FROM visible_ticket t JOIN ticket_message m ON m.ticket_uuid=t.ticket_uuid
        JOIN customer c ON c.customer_uuid=t.customer_uuid AND c.company_uuid=t.company_uuid
        LEFT JOIN staff_user s ON s.staff_user_uuid=m.staff_user_uuid AND s.company_uuid=t.company_uuid
        WHERE t.company_uuid=%s AND t.ticket_uuid=%s AND (%s OR m.message_type<>'memo')
        ORDER BY m.created_at,m.ticket_message_uuid
        """, (company_uuid, str(ticket_uuid), include_memos))
    messages, memos = [], []
    for message in fetch_rows(cursor):
        (memos if message["message_type"] == "memo" else messages).append(message)
    cursor.execute("SELECT " + TICKET_COLUMNS + TICKET_FROM + """
        WHERE t.company_uuid=%s AND t.customer_uuid=%s AND t.ticket_uuid<>%s
        ORDER BY t.created_at,t.ticket_number
    """, (company_uuid, customer_uuid, str(ticket_uuid)))
    related = fetch_rows(cursor)
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
    return {"ticket": ticket, "messages": messages, "memos": memos, "customer_tickets": related,
            "duplicate_candidates": fetch_rows(cursor)}


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

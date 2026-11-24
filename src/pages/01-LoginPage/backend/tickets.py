"""Company-scoped, read-only ticket APIs for the desk."""
import hashlib
import os
from contextlib import closing
from datetime import date
from secrets import token_urlsafe
from typing import Annotated, Literal
from uuid import UUID

import psycopg2
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from psycopg2.extras import RealDictCursor

SESSION_COOKIE = "voc_session"
SESSION_SECONDS = 8 * 60 * 60
STATUSES = {"미처리", "보류", "답변완료"}
SENTIMENTS = {"긍정", "중립", "부정", "매우 부정"}
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
    cat.name AS category, t.category_status, t.sentiment, t.sentiment_status,
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


def fail(status, code, message):
    raise HTTPException(status_code=status, detail={"code": code, "message": message})


def token_digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def issue_session(dsn, email, password_hash):
    """Bind the cookie to the same unique employee and credential that passed verification."""
    token = token_urlsafe(32)
    with closing(psycopg2.connect(dsn, connect_timeout=5)) as connection, connection:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM staff_session WHERE expires_at <= now()")
            cursor.execute("""
                WITH accounts AS (
                    SELECT s.staff_user_uuid, s.password_hash, c.email_domain
                    FROM staff_user s JOIN company c USING(company_uuid)
                    WHERE lower(s.email)=%s LIMIT 2
                )
                INSERT INTO staff_session(token_hash, staff_user_uuid, expires_at)
                SELECT %s, staff_user_uuid, now() + %s * interval '1 second'
                FROM accounts
                WHERE (SELECT count(*) FROM accounts)=1 AND password_hash=%s AND lower(email_domain)=%s
                """, (email, token_digest(token), SESSION_SECONDS, password_hash, email.split("@", 1)[1]))
            if cursor.rowcount != 1:
                fail(401, "INVALID_CREDENTIALS", "이메일 또는 비밀번호를 확인해 주세요.")
    return token


def secure_cookie(request):
    return request.url.scheme == "https" or os.environ.get("SESSION_COOKIE_SECURE", "").lower() == "true"


def filter_ids(conditions, params, column, entries, *, nullable=False):
    if not entries:
        return
    allow_null = nullable and "unassigned" in entries
    try:
        ids = [str(UUID(entry)) for entry in entries if not (nullable and entry == "unassigned")]
    except ValueError:
        fail(422, "INVALID_FILTER", "필터 값을 확인해 주세요.")
    clauses = []
    if ids:
        clauses.append(column + " = ANY(%s::uuid[])")
        params.append(ids)
    if allow_null:
        clauses.append(column + " IS NULL")
    conditions.append("(" + " OR ".join(clauses) + ")")


def register_ticket_routes(app: FastAPI, dsn: str):
    def session(request: Request):
        token = request.cookies.get(SESSION_COOKIE)
        if not token or len(token) > 128:
            fail(401, "LOGIN_REQUIRED", "로그인해 주세요.")
        with closing(psycopg2.connect(dsn, connect_timeout=5)) as connection:
            connection.set_session(readonly=True, isolation_level="REPEATABLE READ")
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute("""
                    SELECT s.staff_user_uuid, s.company_uuid, lower(s.email) AS email,
                           coalesce(s.name, s.email) AS name, EXISTS (
                               SELECT FROM staff_permission sp JOIN permission p USING(permission_uuid)
                               WHERE sp.staff_user_uuid=s.staff_user_uuid AND p.code='ticket.read'
                           ) AS can_read
                    FROM staff_session ses JOIN staff_user s USING(staff_user_uuid)
                    JOIN company c ON c.company_uuid=s.company_uuid
                    WHERE ses.token_hash=%s AND ses.expires_at > now()
                        AND lower(c.email_domain)=lower(split_part(s.email,'@',2))
                    """, (token_digest(token),))
                staff = cursor.fetchone()
                if staff is None:
                    fail(401, "LOGIN_REQUIRED", "로그인해 주세요.")
                yield cursor, staff

    def ticket_access(auth=Depends(session)):
        if not auth[1]["can_read"]:
            fail(403, "TICKET_READ_FORBIDDEN", "티켓 조회 권한이 없습니다.")
        return auth

    @app.get("/api/auth/me")
    def me(auth=Depends(session)):
        staff = auth[1]
        return {"email": staff["email"], "name": staff["name"]}

    @app.post("/api/auth/logout", status_code=204)
    def logout(request: Request, response: Response):
        token = request.cookies.get(SESSION_COOKIE)
        if token and len(token) <= 128:
            with closing(psycopg2.connect(dsn, connect_timeout=5)) as connection, connection:
                with connection.cursor() as cursor:
                    cursor.execute("DELETE FROM staff_session WHERE token_hash=%s", (token_digest(token),))
        response.delete_cookie(SESSION_COOKIE, path="/api", httponly=True, secure=secure_cookie(request), samesite="lax")

    @app.get("/api/tickets/options")
    def options(auth=Depends(ticket_access)):
        cursor, staff = auth
        company = (staff["company_uuid"],)
        cursor.execute("SELECT team_uuid AS id,name FROM team WHERE company_uuid=%s ORDER BY name,id", company)
        teams = cursor.fetchall()
        cursor.execute("""SELECT staff_user_uuid AS id,coalesce(name,email) AS name
                          FROM staff_user WHERE company_uuid=%s ORDER BY name,id""", company)
        assignees = cursor.fetchall()
        cursor.execute("""SELECT category_uuid AS id,cat.name FROM category cat
                          JOIN company c ON c.domain_uuid=cat.domain_uuid
                          WHERE c.company_uuid=%s ORDER BY cat.name,id""", company)
        return {"teams": teams, "assignees": assignees, "categories": cursor.fetchall()}

    @app.get("/api/tickets")
    def list_tickets(
        scope: Literal["mine", "all"] = "all",
        search_by: Literal["title", "customer", "id"] = "title",
        q: Annotated[str, Query(max_length=50)] = "",
        team: Annotated[list[str] | None, Query()] = None,
        assignee: Annotated[list[str] | None, Query()] = None,
        category: Annotated[list[str] | None, Query()] = None,
        status: Annotated[list[str] | None, Query()] = None,
        sentiment: Annotated[list[str] | None, Query()] = None,
        days: int | None = None, start: date | None = None, end: date | None = None,
        sort: str = "default", page: Annotated[int, Query(ge=1)] = 1, page_size: int = 10,
        auth=Depends(ticket_access),
    ):
        cursor, staff = auth
        if sort not in SORTS or page_size not in (10, 20, 30, 50):
            fail(422, "INVALID_PAGE", "정렬 또는 페이지 크기를 확인해 주세요.")
        if (start is None) != (end is None) or (days is not None and start is not None):
            fail(422, "INVALID_PERIOD", "최근 기간 또는 시작일·종료일 한 쌍을 선택해 주세요.")
        conditions = ["t.company_uuid=%s"]
        params = [staff["company_uuid"]]
        if scope == "mine":
            conditions.append("t.assignee_uuid=%s")
            params.append(staff["staff_user_uuid"])
        if q.strip():
            search = {"title": "t.title", "customer": "c.external_customer_id",
                      "id": "('#' || t.ticket_number)"}
            conditions.append(f"strpos(lower({search[search_by]}), lower(%s)) > 0")
            params.append(q.strip())
        filter_ids(conditions, params, "t.assigned_team_uuid", team, nullable=True)
        filter_ids(conditions, params, "t.assignee_uuid", assignee, nullable=True)
        filter_ids(conditions, params, "t.category_uuid", category)
        for entries, allowed, column in ((status, STATUSES, "t.status"), (sentiment, SENTIMENTS, "t.sentiment")):
            if entries:
                if not set(entries) <= allowed:
                    fail(422, "INVALID_FILTER", "필터 값을 확인해 주세요.")
                conditions.append(column + " = ANY(%s)")
                params.append(entries)
        if days is not None:
            if days not in (1, 3, 7, 30):
                fail(422, "INVALID_PERIOD", "최근 기간은 1·3·7·30일 중에서 선택해 주세요.")
            today = "(now() AT TIME ZONE 'Asia/Seoul')::date"
            conditions.extend((f"t.created_at >= (({today} - %s)::timestamp AT TIME ZONE 'Asia/Seoul')",
                               f"t.created_at < (({today} + 1)::timestamp AT TIME ZONE 'Asia/Seoul')"))
            params.append(days - 1)
        if start is not None:
            if not 0 <= (end - start).days < 365:
                fail(422, "INVALID_PERIOD", "시작일과 종료일을 포함해 최대 365일까지 조회할 수 있습니다.")
            conditions.extend(("t.created_at >= (%s::date::timestamp AT TIME ZONE 'Asia/Seoul')",
                               "t.created_at < ((%s::date + 1)::timestamp AT TIME ZONE 'Asia/Seoul')"))
            params.extend((start, end))
        filtered = TICKET_FROM + " WHERE " + " AND ".join(conditions)
        cursor.execute("SELECT count(*) AS total " + filtered, params)
        total = cursor.fetchone()["total"]
        page = min(page, max(1, (total + page_size - 1) // page_size))
        cursor.execute("SELECT " + TICKET_COLUMNS + filtered + " ORDER BY " + SORTS[sort]
                       + ", t.ticket_number ASC LIMIT %s OFFSET %s", [*params, page_size, (page - 1) * page_size])
        return {"items": cursor.fetchall(), "total": total, "page": page, "page_size": page_size}

    @app.get("/api/tickets/{ticket_uuid}")
    def detail(ticket_uuid: UUID, auth=Depends(ticket_access)):
        cursor, staff = auth
        cursor.execute("SELECT " + TICKET_COLUMNS + ", t.customer_uuid " + TICKET_FROM
                       + " WHERE t.ticket_uuid=%s AND t.company_uuid=%s", (str(ticket_uuid), staff["company_uuid"]))
        ticket = cursor.fetchone()
        if ticket is None:
            fail(404, "TICKET_NOT_FOUND", "티켓을 찾을 수 없습니다.")
        cursor.execute("""
            SELECT t.ticket_uuid,t.ticket_number,m.sender_type,m.message_type,m.body,m.created_at
            FROM visible_ticket t JOIN ticket_message m ON m.ticket_uuid=t.ticket_uuid
            WHERE t.company_uuid=%s AND t.customer_uuid=%s
            ORDER BY m.created_at,t.ticket_number,m.ticket_message_uuid
            """, (staff["company_uuid"], ticket.pop("customer_uuid")))
        messages = cursor.fetchall()
        cursor.execute("""
            SELECT peer.ticket_uuid,peer.ticket_number,peer.title,ch.type AS channel
            FROM ticket_duplicate_candidate link
            JOIN visible_ticket peer ON peer.ticket_uuid =
                CASE WHEN link.ticket_uuid=%s THEN link.candidate_ticket_uuid ELSE link.ticket_uuid END
            JOIN channel ch ON ch.channel_uuid=peer.channel_uuid AND ch.company_uuid=peer.company_uuid
            WHERE (link.ticket_uuid=%s OR link.candidate_ticket_uuid=%s)
                AND peer.company_uuid=%s AND ch.type<>%s
            ORDER BY peer.created_at,peer.ticket_number
            """, (str(ticket_uuid), str(ticket_uuid), str(ticket_uuid), staff["company_uuid"], ticket["channel"]))
        return {"ticket": ticket, "messages": messages, "duplicate_candidates": cursor.fetchall()}

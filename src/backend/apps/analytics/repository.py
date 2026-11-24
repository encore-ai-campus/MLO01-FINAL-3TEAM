"""Queries for the staff analytics dashboard."""

from config.database import fetch_rows


def staff_role(cursor, actor):
    # A deployment without the role migration has no analytics administrators.
    cursor.execute("""
        SELECT to_jsonb(staff)->>'role' FROM staff_user AS staff
        WHERE staff.staff_user_uuid = %s AND staff.company_uuid = %s
    """, (actor["staff_user_uuid"], actor["company_uuid"]))
    row = cursor.fetchone()
    return row[0] if row else None


def teams(cursor, company):
    cursor.execute("""
        SELECT team_uuid::text AS id, name FROM team
        WHERE company_uuid = %s ORDER BY name, team_uuid
    """, (company,))
    return fetch_rows(cursor)


SCOPED_TICKETS = """
WITH scoped AS (
    SELECT * FROM visible_ticket
    WHERE company_uuid = %(company)s
      AND (%(team)s::uuid IS NULL OR assigned_team_uuid = %(team)s::uuid)
)
"""


def receipts(cursor, filters):
    cursor.execute(SCOPED_TICKETS + """
        SELECT count(*) FILTER (WHERE created_at >= %(start)s AND created_at < %(end)s) AS received,
               count(*) FILTER (WHERE created_at >= %(compare_start)s AND created_at < %(start)s) AS previous,
               count(*) FILTER (WHERE status <> '답변완료' AND created_at <= %(now)s - interval '24 hours') AS aged,
               count(*) FILTER (WHERE status = '미처리' AND assignee_uuid IS NULL AND created_at < %(now)s) AS unassigned,
               count(*) FILTER (WHERE status = '미처리' AND assignee_uuid IS NOT NULL AND created_at < %(now)s) AS assigned,
               count(*) FILTER (WHERE status = '보류' AND created_at < %(now)s) AS held
        FROM scoped
    """, filters)
    summary = fetch_rows(cursor)[0]
    cursor.execute(SCOPED_TICKETS + """
        SELECT (created_at AT TIME ZONE 'Asia/Seoul')::date AS day, count(*) AS total
        FROM scoped
        WHERE created_at >= %(start)s AND created_at < %(end)s GROUP BY 1
    """, filters)
    summary["daily"] = dict(cursor.fetchall())
    return summary


def sentiments(cursor, filters):
    cursor.execute(SCOPED_TICKETS + """
        SELECT sentiment,
               count(*) FILTER (WHERE created_at >= %(start)s) AS selected,
               count(*) FILTER (WHERE created_at < %(start)s) AS previous
        FROM scoped
        WHERE sentiment IN ('긍정', '중립', '부정', '매우 부정')
          AND created_at >= %(compare_start)s AND created_at < %(end)s
        GROUP BY sentiment
    """, filters)
    return {row["sentiment"]: row for row in fetch_rows(cursor)}


def categories(cursor, filters):
    cursor.execute(SCOPED_TICKETS + """
        SELECT category.name,
               count(*) FILTER (WHERE scoped.created_at >= %(start)s) AS selected,
               count(*) FILTER (WHERE scoped.created_at < %(start)s) AS previous
        FROM scoped LEFT JOIN category
          ON category.category_uuid = scoped.category_uuid AND category.domain_uuid = scoped.domain_uuid
        WHERE scoped.created_at >= %(compare_start)s AND scoped.created_at < %(end)s
        GROUP BY scoped.category_uuid, category.name
        ORDER BY selected DESC, category.name NULLS LAST, scoped.category_uuid
    """, filters)
    return fetch_rows(cursor)

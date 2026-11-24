"""Queries for the staff analytics dashboard."""

import json

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


def hourly_receipts(cursor, filters):
    cursor.execute(SCOPED_TICKETS + """
        SELECT extract(hour FROM created_at AT TIME ZONE 'Asia/Seoul')::int AS hour,
               count(*) FILTER (WHERE created_at >= %(start)s) AS selected,
               count(*) FILTER (WHERE created_at < %(start)s) AS previous
        FROM scoped
        WHERE created_at >= %(compare_start)s AND created_at < %(end)s GROUP BY 1
    """, filters)
    return {row["hour"]: row for row in fetch_rows(cursor)}


def available_sources(cursor):
    cursor.execute("""
        SELECT (to_regclass('ticket_event') IS NOT NULL AND to_regclass('event_type') IS NOT NULL) AS events,
               EXISTS (SELECT FROM pg_attribute WHERE attrelid = to_regclass('ticket_message')
                       AND attname = 'sent_at' AND NOT attisdropped) AS first_answers,
               to_regclass('collab_request') IS NOT NULL AS reviews
    """)
    return fetch_rows(cursor)[0]


def staff_in_scope(cursor, filters):
    cursor.execute("""
        SELECT staff.staff_user_uuid::text AS id, coalesce(staff.name, staff.email) AS name,
               team.name AS team, to_jsonb(staff)->>'role' AS role,
               EXISTS (
                 SELECT FROM staff_permission AS sp JOIN permission USING (permission_uuid)
                 WHERE sp.staff_user_uuid = staff.staff_user_uuid AND permission.code = 'ticket.handle'
               ) AS handles_tickets
        FROM staff_user AS staff
        LEFT JOIN team ON team.team_uuid = staff.team_uuid AND team.company_uuid = staff.company_uuid
        WHERE staff.company_uuid = %(company)s AND to_jsonb(staff)->>'role' IS DISTINCT FROM 'admin'
          AND (%(team)s::uuid IS NULL OR staff.team_uuid = %(team)s::uuid)
        ORDER BY name, staff.staff_user_uuid
    """, filters)
    return fetch_rows(cursor)


def active_assignments(cursor, filters):
    cursor.execute("""
        SELECT assignee_uuid::text, count(*) FILTER (WHERE status <> '답변완료')
        FROM visible_ticket WHERE company_uuid = %(company)s AND assignee_uuid IS NOT NULL
          AND created_at < %(now)s GROUP BY assignee_uuid
    """, filters)
    return dict(cursor.fetchall())


def event_history(cursor, filters):
    cursor.execute("""
        WITH tickets AS (
            SELECT * FROM visible_ticket WHERE company_uuid = %(company)s AND created_at < %(now)s
        ),
        ledger AS (
            SELECT e.*, kind.code
            FROM ticket_event AS e JOIN event_type AS kind USING (event_type_uuid)
            JOIN tickets USING (ticket_uuid)
            WHERE kind.code IN ('assigned', 'transferred', 'answer_completed') AND e.created_at < %(now)s
        ),
        last_assignment AS (
            SELECT DISTINCT ON (ticket_uuid) ticket_uuid, recipient_uuid, code, created_at
            FROM ledger WHERE code IN ('assigned', 'transferred')
            ORDER BY ticket_uuid, created_at DESC, ticket_event_uuid DESC
        ),
        first_completion AS (
            SELECT DISTINCT ON (e.ticket_uuid) e.ticket_uuid, e.actor_uuid, e.created_at
            FROM ledger AS e
            JOIN staff_user AS actor ON actor.staff_user_uuid = e.actor_uuid AND actor.company_uuid = %(company)s
            WHERE e.code = 'answer_completed' AND e.to_status::text IN ('답변완료', 'answered')
              AND COALESCE(e.from_status::text, '') NOT IN ('답변완료', 'answered')
            ORDER BY e.ticket_uuid, e.created_at, e.ticket_event_uuid
        ),
        scoped_completions AS (
            SELECT f.*, t.created_at AS received_at
            FROM first_completion AS f JOIN tickets AS t USING (ticket_uuid)
            WHERE f.created_at >= %(compare_start)s AND f.created_at < %(end)s
              AND (%(team)s::uuid IS NULL OR t.assigned_team_uuid = %(team)s::uuid)
        ),
        daily_completed AS (
            SELECT (created_at AT TIME ZONE 'Asia/Seoul')::date AS day, count(*) AS total
            FROM scoped_completions GROUP BY 1
        ),
        assigned_in_period AS (
            SELECT e.recipient_uuid AS id, min(e.created_at) AS created_at
            FROM ledger AS e WHERE e.recipient_uuid IS NOT NULL AND e.code IN ('assigned', 'transferred')
              AND e.created_at >= %(start)s AND e.created_at < %(end)s
            GROUP BY e.recipient_uuid, e.ticket_uuid
        ),
        staff_flow AS (
            SELECT id, (created_at AT TIME ZONE 'Asia/Seoul')::date AS day, 'assigned' AS kind, count(*) AS total
            FROM assigned_in_period GROUP BY 1, 2
            UNION ALL
            SELECT actor_uuid, (created_at AT TIME ZONE 'Asia/Seoul')::date, 'done', count(*)
            FROM first_completion WHERE created_at >= %(compare_start)s AND created_at < %(end)s GROUP BY 1, 2
        ),
        staff_pending AS (
            SELECT t.assignee_uuid AS id,
                   count(*) FILTER (WHERE t.status <> '답변완료' AND a.code = 'assigned') AS assigned,
                   count(*) FILTER (WHERE t.status <> '답변완료' AND a.code = 'transferred') AS transferred,
                   count(*) FILTER (WHERE t.status <> '답변완료' AND a.code IS NULL) AS unknown,
                   count(*) FILTER (WHERE a.created_at >= date_trunc('day', %(now)s, 'Asia/Seoul')) AS today
            FROM tickets AS t LEFT JOIN last_assignment AS a
              ON a.ticket_uuid = t.ticket_uuid AND a.recipient_uuid = t.assignee_uuid
            WHERE t.assignee_uuid IS NOT NULL GROUP BY t.assignee_uuid
        )
        SELECT jsonb_build_object(
            'daily', COALESCE((SELECT jsonb_agg(daily_completed) FROM daily_completed), '[]'),
            'flow', COALESCE((SELECT jsonb_agg(staff_flow) FROM staff_flow), '[]'),
            'pending', COALESCE((SELECT jsonb_agg(staff_pending) FROM staff_pending), '[]'),
            'resolution', (SELECT percentile_cont(.5) WITHIN GROUP (
                ORDER BY extract(epoch FROM created_at - received_at) / 60
            ) FROM scoped_completions WHERE created_at >= %(start)s AND created_at >= received_at),
            'missing_completions', (
                SELECT count(*) FROM tickets AS t LEFT JOIN first_completion AS f USING (ticket_uuid)
                WHERE t.status = '답변완료' AND f.ticket_uuid IS NULL
                  AND (%(team)s::uuid IS NULL OR t.assigned_team_uuid = %(team)s::uuid)
            )
        )::text
    """, filters)
    return json.loads(cursor.fetchone()[0])


def first_answer_times(cursor, filters):
    cursor.execute("""
        WITH first_answer AS (
            SELECT DISTINCT ON (m.ticket_uuid) m.staff_user_uuid, t.assigned_team_uuid, m.sent_at,
                   extract(epoch FROM m.sent_at - t.created_at) / 60 AS minutes
            FROM ticket_message AS m JOIN visible_ticket AS t USING (ticket_uuid)
            JOIN staff_user AS author ON author.staff_user_uuid = m.staff_user_uuid AND author.company_uuid = t.company_uuid
            WHERE t.company_uuid = %(company)s AND m.sender_type = 'staff' AND m.message_type = 'reply'
              AND m.sent_at >= t.created_at AND m.sent_at < %(end)s
            ORDER BY m.ticket_uuid, m.sent_at, m.ticket_message_uuid
        ), duration AS (
            SELECT * FROM first_answer WHERE sent_at >= %(start)s
        )
        SELECT (
            SELECT percentile_cont(.5) WITHIN GROUP (ORDER BY minutes) FROM duration
            WHERE %(team)s::uuid IS NULL OR assigned_team_uuid = %(team)s::uuid
        ), COALESCE((
            SELECT jsonb_object_agg(id, median) FROM (
                SELECT staff_user_uuid AS id, percentile_cont(.5) WITHIN GROUP (ORDER BY minutes) AS median
                FROM duration GROUP BY staff_user_uuid
            ) AS staff
        ), '{}')::text
    """, filters)
    median, agents = cursor.fetchone()
    return median, json.loads(agents)


def review_pending(cursor, filters):
    cursor.execute("""
        SELECT request.recipient_uuid::text, count(*)
        FROM collab_request AS request JOIN visible_ticket AS ticket USING (ticket_uuid)
        WHERE ticket.company_uuid = %(company)s AND request.request_type::text = 'review'
          AND request.status::text IN ('pending', 'accepted')
        GROUP BY request.recipient_uuid
    """, filters)
    return dict(cursor.fetchall())

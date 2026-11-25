import json

from apps.tickets.action_repository import CONDUCT
from config.database import fetch_rows


TICKET_FIELDS = """
    t.ticket_uuid, t.company_uuid, t.domain_uuid, t.ticket_number, t.status,
    t.category_uuid, t.assigned_team_uuid, t.assignee_uuid, t.category_status,
    t.category_manually_set, t.category_review_reason,
    t.category_applied_body_version, t.category_applied_preprocess_version
"""


def read_ticket(cursor, company_uuid, ticket_uuid):
    cursor.execute(f"""
        SELECT {TICKET_FIELDS} FROM visible_ticket t
        WHERE t.company_uuid=%s AND t.ticket_uuid=%s
    """, (company_uuid, ticket_uuid))
    return next(iter(fetch_rows(cursor)), None)


def lock_result_ticket(cursor, company_uuid, event_id):
    cursor.execute(f"""
        SELECT {TICKET_FIELDS} FROM ticket t
        WHERE t.company_uuid=%s AND t.source_event_id=%s
          AND EXISTS (SELECT FROM visible_ticket v WHERE v.ticket_uuid=t.ticket_uuid)
        FOR UPDATE OF t
    """, (company_uuid, event_id))
    return next(iter(fetch_rows(cursor)), None)


def latest_category_version(cursor, company_uuid, event_id):
    cursor.execute("""
        SELECT o.body_version,o.preprocess_version
        FROM voc.ticket_analysis_outbox o
        JOIN voc.silver_messages s ON s.event_id=o.event_id AND s.schema_version=4
        WHERE o.company_uuid=%s AND o.event_id=%s AND o.analysis_kind='category'
          AND s.message_kind='customer_inquiry' AND s.body_version=o.body_version
        ORDER BY o.preprocess_version DESC LIMIT 1 FOR SHARE OF s
    """, (company_uuid, event_id))
    return cursor.fetchone()


def category_choices(cursor, domain_uuid):
    cursor.execute("""
        SELECT category_uuid,name FROM category WHERE domain_uuid=%s ORDER BY name,category_uuid
    """, (domain_uuid,))
    return fetch_rows(cursor)


def category_team(cursor, ticket, category_uuid):
    cursor.execute("""
        SELECT r.team_uuid FROM category c LEFT JOIN ticket_category_team_route r
          ON r.domain_uuid=c.domain_uuid AND r.category_uuid=c.category_uuid AND r.company_uuid=%s
        WHERE c.domain_uuid=%s AND c.category_uuid=%s
    """, (ticket["company_uuid"], ticket["domain_uuid"], category_uuid))
    return cursor.fetchone()


def update_category(cursor, ticket_uuid, category_uuid, team_uuid, assignee_uuid, review_reason, version):
    cursor.execute("""
        UPDATE ticket SET category_uuid=%s, category_status='succeeded', assigned_team_uuid=%s,
            assignee_uuid=%s, category_review_reason=%s,
            category_applied_body_version=%s, category_applied_preprocess_version=%s, updated_at=now()
        WHERE ticket_uuid=%s
    """, (category_uuid, team_uuid, assignee_uuid, review_reason, *version, ticket_uuid))


def save_manual_category(cursor, ticket_uuid, category_uuid):
    cursor.execute("""
        UPDATE ticket SET category_uuid=%s, category_status='succeeded', category_review_reason=NULL,
            category_manually_set=true, updated_at=now()
        WHERE ticket_uuid=%s
    """, (category_uuid, ticket_uuid))


def record_event(cursor, ticket_uuid, code, *, actor=None, recipient=None, reason=None, details=None):
    cursor.execute("""
        INSERT INTO ticket_event(ticket_uuid,event_type_uuid,actor_uuid,recipient_uuid,transfer_reason,details)
        SELECT %s,event_type_uuid,%s,%s,%s,%s::jsonb FROM event_type WHERE code=%s
    """, (ticket_uuid, actor, recipient, reason, json.dumps(details or {}, default=str), code))


def team_policies(cursor, company_uuid, team_uuid):
    cursor.execute("""
        SELECT FROM team WHERE company_uuid=%s AND team_uuid=%s FOR NO KEY UPDATE
    """, (company_uuid, team_uuid))
    if cursor.fetchone() is None:
        return []
    cursor.execute(f"""
        SELECT s.staff_user_uuid, p.batch_size, p.refill_mode, work.active_count
        FROM staff_user s JOIN staff_assignment_policy p USING(company_uuid,staff_user_uuid)
        CROSS JOIN LATERAL (
            SELECT count(*) AS active_count FROM visible_ticket t
            WHERE t.company_uuid=s.company_uuid AND t.assignee_uuid=s.staff_user_uuid AND t.status<>'답변완료'
        ) work
        WHERE s.company_uuid=%s AND s.team_uuid=%s AND p.enabled AND {CONDUCT}
        ORDER BY work.active_count,
            (SELECT max(ev.created_at) FROM ticket_event ev JOIN event_type et USING(event_type_uuid)
             WHERE ev.recipient_uuid=s.staff_user_uuid AND et.code IN ('assigned','ticket_transfer'))
                NULLS FIRST, s.staff_user_uuid
    """, (company_uuid, team_uuid))
    return fetch_rows(cursor)


def fill_staff_batch(cursor, company_uuid, team_uuid, staff_uuid, slots):
    cursor.execute("""
        WITH waiting AS (
            SELECT t.ticket_uuid FROM ticket t
            WHERE t.company_uuid=%s AND t.assigned_team_uuid=%s AND t.assignee_uuid IS NULL
              AND t.status='미처리' AND t.category_status='succeeded' AND t.category_uuid IS NOT NULL
              AND t.category_review_reason IS NULL
              AND EXISTS (SELECT FROM visible_ticket v WHERE v.ticket_uuid=t.ticket_uuid)
            ORDER BY t.created_at,t.ticket_number LIMIT %s FOR UPDATE OF t SKIP LOCKED
        ), assigned AS (
            UPDATE ticket SET assignee_uuid=%s,updated_at=now()
            WHERE ticket_uuid IN (SELECT ticket_uuid FROM waiting) RETURNING ticket_uuid
        )
        INSERT INTO ticket_event(ticket_uuid,event_type_uuid,recipient_uuid,details)
        SELECT ticket_uuid,event_type_uuid,%s,jsonb_build_object('team_uuid',%s::uuid)
        FROM assigned CROSS JOIN (SELECT event_type_uuid FROM event_type WHERE code='assigned') ev
    """, (company_uuid, team_uuid, slots, staff_uuid, staff_uuid, team_uuid))
    return cursor.rowcount


def routing_options(cursor, company_uuid):
    cursor.execute("""
        SELECT c.category_uuid,c.name,r.team_uuid,t.name AS team_name FROM company org JOIN category c USING(domain_uuid)
        LEFT JOIN ticket_category_team_route r ON (r.company_uuid,r.category_uuid)=(org.company_uuid,c.category_uuid)
        LEFT JOIN team t ON (t.company_uuid,t.team_uuid)=(r.company_uuid,r.team_uuid)
        WHERE org.company_uuid=%s ORDER BY c.name,c.category_uuid
    """, (company_uuid,))
    categories = fetch_rows(cursor)
    cursor.execute("""
        SELECT team_uuid,name FROM team WHERE company_uuid=%s ORDER BY name,team_uuid
    """, (company_uuid,))
    return categories, fetch_rows(cursor)


def save_route(cursor, company_uuid, category_uuid, team_uuid):
    if team_uuid is None:
        cursor.execute("""
            DELETE FROM ticket_category_team_route WHERE company_uuid=%s AND category_uuid=%s
        """, (company_uuid, category_uuid))
        return
    cursor.execute("""
        INSERT INTO ticket_category_team_route(company_uuid,domain_uuid,category_uuid,team_uuid)
        SELECT org.company_uuid,org.domain_uuid,c.category_uuid,t.team_uuid
        FROM company org JOIN category c USING(domain_uuid) JOIN team t USING(company_uuid)
        WHERE org.company_uuid=%s AND c.category_uuid=%s AND t.team_uuid=%s
        ON CONFLICT (company_uuid,category_uuid) DO UPDATE SET team_uuid=excluded.team_uuid
    """, (company_uuid, category_uuid, team_uuid))

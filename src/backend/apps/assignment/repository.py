import json

from apps.tickets.action_repository import CONDUCT
from config.database import fetch_rows


TICKET_FIELDS = """
    t.ticket_id, t.company_id, t.domain_id, t.ticket_number, t.status,
    t.category_id, t.assigned_team_id, t.assignee_id, t.category_status, t.sentiment, t.sentiment_status,
    t.is_category_manual, t.category_review_reason,
    t.category_applied_body_version, t.category_applied_preprocess_version
"""


def read_ticket(cursor, company_id, ticket_id):
    cursor.execute(f"""
        SELECT {TICKET_FIELDS} FROM visible_ticket t
        WHERE t.company_id=%s AND t.ticket_id=%s
    """, (company_id, ticket_id))
    return next(iter(fetch_rows(cursor)), None)


def lock_result_ticket(cursor, company_id, event_id):
    cursor.execute(f"""
        SELECT {TICKET_FIELDS} FROM ticket t
        WHERE t.company_id=%s AND t.source_event_id=%s
          AND EXISTS (SELECT FROM visible_ticket v WHERE v.ticket_id=t.ticket_id)
        FOR UPDATE OF t
    """, (company_id, event_id))
    return next(iter(fetch_rows(cursor)), None)


def latest_category_version(cursor, company_id, event_id):
    cursor.execute("""
        SELECT o.body_version,o.preprocess_version
        FROM voc.ticket_analysis_outbox o
        JOIN voc.silver_message s ON s.event_id=o.event_id AND s.schema_version=4
        WHERE o.company_id=%s AND o.event_id=%s AND o.analysis_kind='category'
          AND s.message_kind='customer_inquiry' AND s.body_version=o.body_version
        ORDER BY o.preprocess_version DESC LIMIT 1 FOR SHARE OF s
    """, (company_id, event_id))
    return cursor.fetchone()


def category_choices(cursor, domain_id):
    cursor.execute("""
        SELECT category_id,name FROM category WHERE domain_id=%s ORDER BY name,category_id
    """, (domain_id,))
    return fetch_rows(cursor)


def category_team(cursor, ticket, category_id):
    cursor.execute("""
        SELECT r.team_id FROM category c LEFT JOIN ticket_category_team_route r
          ON r.domain_id=c.domain_id AND r.category_id=c.category_id AND r.company_id=%s
        WHERE c.domain_id=%s AND c.category_id=%s
    """, (ticket["company_id"], ticket["domain_id"], category_id))
    return cursor.fetchone()


def update_category(cursor, ticket_id, category_id, team_id, assignee_id, review_reason, version):
    cursor.execute("""
        UPDATE ticket SET category_id=%s, category_status='succeeded', assigned_team_id=%s,
            assignee_id=%s, category_review_reason=%s,
            category_applied_body_version=%s, category_applied_preprocess_version=%s, updated_at=now()
        WHERE ticket_id=%s
    """, (category_id, team_id, assignee_id, review_reason, *version, ticket_id))


def save_manual_category(cursor, ticket_id, category_id):
    cursor.execute("""
        UPDATE ticket SET category_id=%s, category_status='succeeded', category_review_reason=NULL,
            is_category_manual=true, updated_at=now()
        WHERE ticket_id=%s
    """, (category_id, ticket_id))


def save_manual_sentiment(cursor, ticket_id, sentiment):
    cursor.execute("""
        UPDATE ticket SET sentiment=%s,sentiment_status='succeeded',updated_at=now() WHERE ticket_id=%s
    """, (sentiment, ticket_id))


def reassign_team(cursor, ticket_id, team_id):
    cursor.execute("""
        UPDATE ticket SET assigned_team_id=%s,assignee_id=NULL,updated_at=now(),
            category_review_reason=NULLIF(category_review_reason,'no_team_mapping')
        WHERE ticket_id=%s
    """, (team_id, ticket_id))


def record_manual_event(cursor, ticket_id, code, *, actor, reason, details):
    cursor.execute("INSERT INTO event_type(code) VALUES (%s) ON CONFLICT (code) DO NOTHING", (code,))
    return record_event(cursor, ticket_id, code, actor=actor, reason=reason, details=details)


def record_event(cursor, ticket_id, code, *, actor=None, recipient=None, reason=None, details=None):
    cursor.execute("""
        INSERT INTO ticket_event(ticket_id,event_type_id,actor_id,recipient_id,transfer_reason,detail_json,created_at)
        SELECT %s,event_type_id,%s,%s,%s,%s::jsonb,clock_timestamp() FROM event_type WHERE code=%s
        RETURNING ticket_event_id
    """, (ticket_id, actor, recipient, reason, json.dumps(details or {}, default=str), code))
    row = cursor.fetchone()
    return row[0] if row else None


def record_training_review(cursor, feature_snapshot_id, reviewer_id, *, sentiment, category_id, urgent, evidence):
    cursor.execute("""
        INSERT INTO voc.counselor_review(feature_snapshot_id,reviewer_id,sentiment,category_id,is_urgent,evidence_ref)
        VALUES (%s,%s,%s,%s,%s,%s)
    """, (feature_snapshot_id, reviewer_id, sentiment, category_id, urgent, str(evidence)))


def team_policies(cursor, company_id, team_id):
    cursor.execute("""
        SELECT FROM team WHERE company_id=%s AND team_id=%s FOR NO KEY UPDATE
    """, (company_id, team_id))
    if cursor.fetchone() is None:
        return []
    cursor.execute(f"""
        SELECT s.staff_user_id, p.batch_size, p.refill_mode, work.active_count
        FROM staff_user s JOIN staff_assignment_policy p USING(company_id,staff_user_id)
        CROSS JOIN LATERAL (
            SELECT count(*) AS active_count FROM visible_ticket t
            WHERE t.company_id=s.company_id AND t.assignee_id=s.staff_user_id AND t.status<>'답변완료'
        ) work
        WHERE s.company_id=%s AND s.team_id=%s AND p.is_enabled AND {CONDUCT}
        ORDER BY work.active_count,
            (SELECT max(ev.created_at) FROM ticket_event ev JOIN event_type et USING(event_type_id)
             WHERE ev.recipient_id=s.staff_user_id AND et.code IN ('assigned','ticket_transfer'))
                NULLS FIRST, s.staff_user_id
    """, (company_id, team_id))
    return fetch_rows(cursor)


def fill_staff_batch(cursor, company_id, team_id, staff_uuid, slots):
    cursor.execute("""
        WITH waiting AS (
            SELECT t.ticket_id FROM ticket t
            WHERE t.company_id=%s AND t.assigned_team_id=%s AND t.assignee_id IS NULL
              AND t.status='미처리' AND t.category_status='succeeded' AND t.category_id IS NOT NULL
              AND t.category_review_reason IS NULL
              AND EXISTS (SELECT FROM visible_ticket v WHERE v.ticket_id=t.ticket_id)
            ORDER BY t.created_at,t.ticket_number LIMIT %s FOR UPDATE OF t SKIP LOCKED
        ), assigned AS (
            UPDATE ticket SET assignee_id=%s,updated_at=now()
            WHERE ticket_id IN (SELECT ticket_id FROM waiting) RETURNING ticket_id
        )
        INSERT INTO ticket_event(ticket_id,event_type_id,recipient_id,detail_json)
        SELECT ticket_id,event_type_id,%s,jsonb_build_object('team_id',%s::uuid)
        FROM assigned CROSS JOIN (SELECT event_type_id FROM event_type WHERE code='assigned') ev
    """, (company_id, team_id, slots, staff_uuid, staff_uuid, team_id))
    return cursor.rowcount


def routing_options(cursor, company_id):
    cursor.execute("""
        SELECT c.category_id,c.name,r.team_id,t.name AS team_name FROM company org JOIN category c USING(domain_id)
        LEFT JOIN ticket_category_team_route r ON (r.company_id,r.category_id)=(org.company_id,c.category_id)
        LEFT JOIN team t ON (t.company_id,t.team_id)=(r.company_id,r.team_id)
        WHERE org.company_id=%s ORDER BY c.name,c.category_id
    """, (company_id,))
    return fetch_rows(cursor), team_choices(cursor, company_id)


def team_choices(cursor, company_id):
    cursor.execute("""
        SELECT team_id,name FROM team WHERE company_id=%s ORDER BY name,team_id
    """, (company_id,))
    return fetch_rows(cursor)


def save_route(cursor, company_id, category_id, team_id):
    if team_id is None:
        cursor.execute("""
            DELETE FROM ticket_category_team_route WHERE company_id=%s AND category_id=%s
        """, (company_id, category_id))
        return
    cursor.execute("""
        INSERT INTO ticket_category_team_route(company_id,domain_id,category_id,team_id)
        SELECT org.company_id,org.domain_id,c.category_id,t.team_id
        FROM company org JOIN category c USING(domain_id) JOIN team t USING(company_id)
        WHERE org.company_id=%s AND c.category_id=%s AND t.team_id=%s
        ON CONFLICT (company_id,category_id) DO UPDATE SET team_id=excluded.team_id
    """, (company_id, category_id, team_id))

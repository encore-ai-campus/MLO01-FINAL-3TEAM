import hashlib
import json
from secrets import token_urlsafe

from django.db import connection, transaction

from config.database import fetch_rows

SESSION_SECONDS = 8 * 60 * 60
DASHBOARD_READ = "analytics.read"

# Legacy read-only grants and existing two-grant counselor accounts remain valid.
# A super admin can also open tickets through the work permission exposed in settings.
TICKET_READ = """
    EXISTS (
        SELECT FROM staff_permission sp JOIN permission p USING(permission_id)
        WHERE sp.staff_user_id=s.staff_user_id AND (
            p.code='ticket.read' OR (p.code='ticket.handle' AND to_jsonb(s)->>'role'='super_admin')
        )
    )
"""


def token_digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def find_password_hash(email):
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT s.password_hash, c.email_domain
            FROM staff_user s JOIN company c USING(company_id)
            WHERE lower(s.email) = %s LIMIT 2
        """, (email,))
        rows = cursor.fetchall()
    if len(rows) == 1:
        password_hash, domain = rows[0]
        if domain and domain.lower() == email.split("@", 1)[1] and (password_hash or "").startswith("$argon2id$"):
            return password_hash
    return None


def issue_session(email, password_hash):
    token = token_urlsafe(32)
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("DELETE FROM staff_session WHERE expires_at <= now()")
        cursor.execute("""
            WITH accounts AS (
                SELECT s.staff_user_id, s.password_hash, c.email_domain
                FROM staff_user s JOIN company c USING(company_id)
                WHERE lower(s.email)=%s LIMIT 2
            )
            INSERT INTO staff_session(token_hash, staff_user_id, expires_at)
            SELECT %s, staff_user_id, now() + %s * interval '1 second' FROM accounts
            WHERE (SELECT count(*) FROM accounts)=1 AND password_hash=%s AND lower(email_domain)=%s
        """, (email, token_digest(token), SESSION_SECONDS, password_hash, email.split("@", 1)[1]))
        return token if cursor.rowcount == 1 else None


def find_staff(cursor, token):
    cursor.execute(f"""
        SELECT s.staff_user_id, s.company_id, lower(s.email) AS email,
               coalesce(s.name, s.email) AS name,
               to_jsonb(s)->>'role' AS role,
               to_jsonb(s)->>'display_preferences_json' AS display_preferences_json,
               to_jsonb(s)->>'team_id' AS team_id,
               {TICKET_READ} AS can_read,
               -- Before settings setup the permission does not exist and admins have dashboard access.
               CASE WHEN dashboard.permission_id IS NULL THEN coalesce(to_jsonb(s)->>'role' IN ('admin','super_admin'), false)
                    ELSE dashboard_grant.staff_user_id IS NOT NULL END AS can_read_dashboard
        FROM staff_session ses JOIN staff_user s USING(staff_user_id)
        JOIN company c ON c.company_id=s.company_id
        LEFT JOIN permission dashboard ON dashboard.code=%s
        LEFT JOIN staff_permission dashboard_grant ON dashboard_grant.permission_id=dashboard.permission_id
            AND dashboard_grant.staff_user_id=s.staff_user_id
        WHERE ses.token_hash=%s AND ses.expires_at > now()
          AND lower(c.email_domain)=lower(split_part(s.email,'@',2))
    """, (DASHBOARD_READ, token_digest(token)))
    actor = next(iter(fetch_rows(cursor)), None)
    if actor:
        actor["display_preferences_json"] = json.loads(actor["display_preferences_json"] or "{}")
    return actor


def company_staff(cursor, company_id, *, include_super_admin=True, staff_uuid=None, lock=False):
    query = """
        SELECT s.staff_user_id, coalesce(s.name,s.email) AS name, s.email, t.name AS team
        FROM staff_user s LEFT JOIN team t ON t.company_id=s.company_id AND t.team_id=s.team_id
        WHERE s.company_id=%s
    """
    params = [company_id]
    if not include_super_admin:
        query += " AND s.role IS DISTINCT FROM 'super_admin'"
    if staff_uuid is not None:
        query += " AND s.staff_user_id=%s"
        params.append(staff_uuid)
    query += " ORDER BY name,s.staff_user_id"
    if lock:
        query += " FOR UPDATE OF s"
    cursor.execute(query, params)
    return fetch_rows(cursor)


def company_employee(cursor, company_id, staff_uuid, *, lock=False):
    query = """
        SELECT staff_user_id, coalesce(name,email) AS name, email, role
        FROM staff_user WHERE company_id=%s AND staff_user_id=%s
    """
    cursor.execute(query + (" FOR UPDATE" if lock else ""), (company_id, staff_uuid))
    return next(iter(fetch_rows(cursor)), None)


def work_permissions(cursor, company_id, staff_uuid, codes):
    cursor.execute("""
        SELECT p.code FROM permission p
        JOIN staff_permission sp USING(permission_id)
        JOIN staff_user s USING(staff_user_id)
        WHERE s.company_id=%s AND s.staff_user_id=%s AND p.code=ANY(%s)
        ORDER BY p.code
    """, (company_id, staff_uuid, list(codes)))
    return [row[0] for row in cursor.fetchall()]


def set_work_permissions(cursor, company_id, staff_uuid, managed_codes, granted_codes):
    cursor.execute("SELECT code FROM permission WHERE code=ANY(%s) FOR KEY SHARE", (list(managed_codes),))
    if len(cursor.fetchall()) != len(managed_codes):
        return False
    cursor.execute("""
        DELETE FROM staff_permission sp USING permission p, staff_user s
        WHERE sp.permission_id=p.permission_id AND sp.staff_user_id=s.staff_user_id
          AND s.company_id=%s AND s.staff_user_id=%s
          AND p.code=ANY(%s) AND NOT (p.code=ANY(%s))
    """, (company_id, staff_uuid, list(managed_codes), list(granted_codes)))
    cursor.execute("""
        INSERT INTO staff_permission(staff_user_id,permission_id)
        SELECT s.staff_user_id,p.permission_id FROM staff_user s CROSS JOIN permission p
        WHERE s.company_id=%s AND s.staff_user_id=%s AND p.code=ANY(%s)
        ON CONFLICT DO NOTHING
    """, (company_id, staff_uuid, list(granted_codes)))
    return True


def revoke(token):
    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM staff_session WHERE token_hash=%s", (token_digest(token),))

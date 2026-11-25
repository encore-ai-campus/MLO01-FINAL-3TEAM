import hashlib
import json
from secrets import token_urlsafe

from django.db import connection, transaction

from config.database import fetch_rows

SESSION_SECONDS = 8 * 60 * 60
DASHBOARD_READ = "analytics.read"


def token_digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def find_password_hash(email):
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT s.password_hash, c.email_domain
            FROM staff_user s JOIN company c USING(company_uuid)
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
                SELECT s.staff_user_uuid, s.password_hash, c.email_domain
                FROM staff_user s JOIN company c USING(company_uuid)
                WHERE lower(s.email)=%s LIMIT 2
            )
            INSERT INTO staff_session(token_hash, staff_user_uuid, expires_at)
            SELECT %s, staff_user_uuid, now() + %s * interval '1 second' FROM accounts
            WHERE (SELECT count(*) FROM accounts)=1 AND password_hash=%s AND lower(email_domain)=%s
        """, (email, token_digest(token), SESSION_SECONDS, password_hash, email.split("@", 1)[1]))
        return token if cursor.rowcount == 1 else None


def find_staff(cursor, token):
    cursor.execute("""
        SELECT s.staff_user_uuid, s.company_uuid, lower(s.email) AS email,
               coalesce(s.name, s.email) AS name,
               to_jsonb(s)->>'role' AS role,
               to_jsonb(s)->>'display_preferences' AS display_preferences,
               to_jsonb(s)->>'team_uuid' AS team_uuid,
               EXISTS (
                   SELECT FROM staff_permission sp JOIN permission p USING(permission_uuid)
                   WHERE sp.staff_user_uuid=s.staff_user_uuid AND p.code='ticket.read'
               ) AS can_read,
               -- Before settings setup the permission does not exist and admins have dashboard access.
               CASE WHEN dashboard.permission_uuid IS NULL THEN coalesce(to_jsonb(s)->>'role' = 'admin', false)
                    ELSE dashboard_grant.staff_user_uuid IS NOT NULL END AS can_read_dashboard
        FROM staff_session ses JOIN staff_user s USING(staff_user_uuid)
        JOIN company c ON c.company_uuid=s.company_uuid
        LEFT JOIN permission dashboard ON dashboard.code=%s
        LEFT JOIN staff_permission dashboard_grant ON dashboard_grant.permission_uuid=dashboard.permission_uuid
            AND dashboard_grant.staff_user_uuid=s.staff_user_uuid
        WHERE ses.token_hash=%s AND ses.expires_at > now()
          AND lower(c.email_domain)=lower(split_part(s.email,'@',2))
    """, (DASHBOARD_READ, token_digest(token)))
    actor = next(iter(fetch_rows(cursor)), None)
    if actor:
        actor["display_preferences"] = json.loads(actor["display_preferences"] or "{}")
    return actor


def company_staff(cursor, company_uuid):
    cursor.execute("""
        SELECT s.staff_user_uuid, coalesce(s.name,s.email) AS name, s.email, t.name AS team
        FROM staff_user s LEFT JOIN team t ON t.company_uuid=s.company_uuid AND t.team_uuid=s.team_uuid
        WHERE s.company_uuid=%s ORDER BY name,s.staff_user_uuid
    """, (company_uuid,))
    return fetch_rows(cursor)


def company_employee(cursor, company_uuid, staff_uuid, *, lock=False):
    query = """
        SELECT staff_user_uuid, coalesce(name,email) AS name, email
        FROM staff_user WHERE company_uuid=%s AND staff_user_uuid=%s
    """
    cursor.execute(query + (" FOR UPDATE" if lock else ""), (company_uuid, staff_uuid))
    return next(iter(fetch_rows(cursor)), None)


def work_permissions(cursor, company_uuid, staff_uuid, codes):
    cursor.execute("""
        SELECT p.code FROM permission p
        JOIN staff_permission sp USING(permission_uuid)
        JOIN staff_user s USING(staff_user_uuid)
        WHERE s.company_uuid=%s AND s.staff_user_uuid=%s AND p.code=ANY(%s)
        ORDER BY p.code
    """, (company_uuid, staff_uuid, list(codes)))
    return [row[0] for row in cursor.fetchall()]


def set_work_permissions(cursor, company_uuid, staff_uuid, managed_codes, granted_codes):
    cursor.execute("SELECT code FROM permission WHERE code=ANY(%s) FOR KEY SHARE", (list(managed_codes),))
    if len(cursor.fetchall()) != len(managed_codes):
        return False
    cursor.execute("""
        DELETE FROM staff_permission sp USING permission p, staff_user s
        WHERE sp.permission_uuid=p.permission_uuid AND sp.staff_user_uuid=s.staff_user_uuid
          AND s.company_uuid=%s AND s.staff_user_uuid=%s
          AND p.code=ANY(%s) AND NOT (p.code=ANY(%s))
    """, (company_uuid, staff_uuid, list(managed_codes), list(granted_codes)))
    cursor.execute("""
        INSERT INTO staff_permission(staff_user_uuid,permission_uuid)
        SELECT s.staff_user_uuid,p.permission_uuid FROM staff_user s CROSS JOIN permission p
        WHERE s.company_uuid=%s AND s.staff_user_uuid=%s AND p.code=ANY(%s)
        ON CONFLICT DO NOTHING
    """, (company_uuid, staff_uuid, list(granted_codes)))
    return True


def revoke(token):
    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM staff_session WHERE token_hash=%s", (token_digest(token),))

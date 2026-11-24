import hashlib
from secrets import token_urlsafe

from django.db import connection, transaction

from config.database import fetch_rows

SESSION_SECONDS = 8 * 60 * 60


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
               coalesce(s.name, s.email) AS name, EXISTS (
                   SELECT FROM staff_permission sp JOIN permission p USING(permission_uuid)
                   WHERE sp.staff_user_uuid=s.staff_user_uuid AND p.code='ticket.read'
               ) AS can_read
        FROM staff_session ses JOIN staff_user s USING(staff_user_uuid)
        JOIN company c ON c.company_uuid=s.company_uuid
        WHERE ses.token_hash=%s AND ses.expires_at > now()
          AND lower(c.email_domain)=lower(split_part(s.email,'@',2))
    """, (token_digest(token),))
    return next(iter(fetch_rows(cursor)), None)


def revoke(token):
    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM staff_session WHERE token_hash=%s", (token_digest(token),))

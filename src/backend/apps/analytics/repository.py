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

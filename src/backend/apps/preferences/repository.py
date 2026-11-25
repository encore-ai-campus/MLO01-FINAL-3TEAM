import json

from config.database import fetch_rows


def save_display(cursor, actor, preferences):
    cursor.execute("""
        UPDATE staff_user SET display_preferences=display_preferences || %s::jsonb
        WHERE company_uuid=%s AND staff_user_uuid=%s
    """, (json.dumps(preferences), actor["company_uuid"], actor["staff_user_uuid"]))


def categories(cursor, company_uuid):
    cursor.execute("""
        SELECT k.category_uuid, k.name FROM company c JOIN category k USING(domain_uuid)
        WHERE c.company_uuid=%s ORDER BY k.name,k.category_uuid
    """, (company_uuid,))
    return cursor.fetchall()


def visible_replies(cursor, actor, *, reply_uuid=None, scope=None, query="", category=None, lock=False):
    cursor.execute("""
        SELECT r.reply_uuid, r.category_uuid::text AS category_uuid, r.name, r.body, r.updated_at,
               k.name AS category_name, CASE WHEN r.team_uuid IS NULL THEN 'personal' ELSE 'team' END AS scope
        FROM quick_reply r JOIN category k USING(domain_uuid,category_uuid)
        WHERE r.company_uuid=%s
          AND ((r.team_uuid IS NULL AND r.owner_uuid=%s) OR r.team_uuid=%s)
          AND (%s::uuid IS NULL OR r.reply_uuid=%s)
          AND (%s::text IS NULL OR (r.team_uuid IS NULL)=%s)
          AND strpos(lower(r.name),lower(%s)) > 0
          AND (%s::uuid IS NULL OR r.category_uuid=%s)
        ORDER BY r.updated_at DESC,r.reply_uuid
    """ + (" FOR UPDATE OF r" if lock else ""), (
        actor["company_uuid"], actor["staff_user_uuid"], actor.get("team_uuid"),
        reply_uuid, reply_uuid, scope, scope == "personal", query, category, category,
    ))
    return fetch_rows(cursor)


def create_reply(cursor, actor, data, team_uuid):
    owner_uuid = None if team_uuid else actor["staff_user_uuid"]
    cursor.execute("""
        INSERT INTO quick_reply(company_uuid,owner_uuid,team_uuid,domain_uuid,category_uuid,name,body)
        SELECT c.company_uuid,%s,%s,c.domain_uuid,k.category_uuid,%s,%s
        FROM company c JOIN category k USING(domain_uuid)
        WHERE c.company_uuid=%s AND k.category_uuid=%s
        RETURNING reply_uuid
    """, (owner_uuid, team_uuid, data["name"], data["body"],
          actor["company_uuid"], data["category_uuid"]))
    row = cursor.fetchone()
    return row[0] if row else None


def update_reply(cursor, actor, reply_uuid, data):
    cursor.execute("""
        UPDATE quick_reply r SET name=%s,body=%s,domain_uuid=c.domain_uuid,
                                 category_uuid=k.category_uuid,updated_at=now()
        FROM company c JOIN category k USING(domain_uuid)
        WHERE r.company_uuid=c.company_uuid AND c.company_uuid=%s AND r.reply_uuid=%s AND k.category_uuid=%s
    """, (data["name"], data["body"], actor["company_uuid"], reply_uuid, data["category_uuid"]))
    return cursor.rowcount == 1


def delete_reply(cursor, company_uuid, reply_uuid):
    cursor.execute("DELETE FROM quick_reply WHERE company_uuid=%s AND reply_uuid=%s", (company_uuid, reply_uuid))


def company_teams(cursor, company_uuid):
    cursor.execute("SELECT team_uuid,name FROM team WHERE company_uuid=%s ORDER BY name,team_uuid", (company_uuid,))
    return cursor.fetchall()


def active_messenger(cursor, company_uuid):
    cursor.execute("SELECT messenger_provider FROM company WHERE company_uuid=%s", (company_uuid,))
    return cursor.fetchone()[0]


def messenger_routes(cursor, company_uuid, provider):
    cursor.execute("""
        SELECT d.channel, d.team_uuid, d.staff_user_uuid, coalesce(t.name,s.name,s.email) AS target_name
        FROM messenger_destination d
        LEFT JOIN team t USING(company_uuid,team_uuid)
        LEFT JOIN staff_user s USING(company_uuid,staff_user_uuid)
        WHERE d.company_uuid=%s AND d.provider=%s
        ORDER BY target_name NULLS FIRST,d.team_uuid,d.staff_user_uuid
    """, (company_uuid, provider))
    return fetch_rows(cursor)


def save_messenger_route(cursor, company_uuid, data):
    provider, channel = data["provider"], data["channel"]
    team = data["target"] if data["destination"] == "team" else None
    staff = data["target"] if data["destination"] == "staff" else None
    if data["destination"] == "default":
        cursor.execute("UPDATE company SET messenger_provider=%s WHERE company_uuid=%s", (provider, company_uuid))
    if channel:
        cursor.execute("""
            INSERT INTO messenger_destination(company_uuid,provider,team_uuid,staff_user_uuid,channel)
            VALUES (%s,%s,%s,%s,%s)
            ON CONFLICT (company_uuid,provider,team_uuid,staff_user_uuid) DO UPDATE SET channel=excluded.channel
        """, (company_uuid, provider, team, staff, channel))
    else:
        cursor.execute("""
            DELETE FROM messenger_destination WHERE company_uuid=%s AND provider=%s
              AND team_uuid IS NOT DISTINCT FROM %s::uuid AND staff_user_uuid IS NOT DISTINCT FROM %s::uuid
        """, (company_uuid, provider, team, staff))

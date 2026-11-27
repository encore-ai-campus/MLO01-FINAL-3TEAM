import json

from config.database import fetch_rows


def save_display(cursor, actor, preferences):
    cursor.execute("""
        UPDATE staff_user SET display_preferences_json=display_preferences_json || %s::jsonb
        WHERE company_id=%s AND staff_user_id=%s
    """, (json.dumps(preferences), actor["company_id"], actor["staff_user_id"]))


def categories(cursor, company_id):
    cursor.execute("""
        SELECT k.category_id, k.name FROM company c JOIN category k USING(domain_id)
        WHERE c.company_id=%s ORDER BY k.name,k.category_id
    """, (company_id,))
    return cursor.fetchall()


def visible_replies(cursor, actor, *, reply_id=None, scope=None, query="", category=None, lock=False):
    cursor.execute("""
        SELECT r.reply_id, r.category_id::text AS category_id, r.name, r.body, r.updated_at,
               k.name AS category_name, CASE WHEN r.team_id IS NULL THEN 'personal' ELSE 'team' END AS scope
        FROM quick_reply r JOIN category k USING(domain_id,category_id)
        WHERE r.company_id=%s
          AND ((r.team_id IS NULL AND r.owner_id=%s) OR r.team_id=%s)
          AND (%s::uuid IS NULL OR r.reply_id=%s)
          AND (%s::text IS NULL OR (r.team_id IS NULL)=%s)
          AND strpos(lower(r.name),lower(%s)) > 0
          AND (%s::uuid IS NULL OR r.category_id=%s)
        ORDER BY r.updated_at DESC,r.reply_id
    """ + (" FOR UPDATE OF r" if lock else ""), (
        actor["company_id"], actor["staff_user_id"], actor.get("team_id"),
        reply_id, reply_id, scope, scope == "personal", query, category, category,
    ))
    return fetch_rows(cursor)


def create_reply(cursor, actor, data, team_id):
    owner_id = None if team_id else actor["staff_user_id"]
    cursor.execute("""
        INSERT INTO quick_reply(company_id,owner_id,team_id,domain_id,category_id,name,body)
        SELECT c.company_id,%s,%s,c.domain_id,k.category_id,%s,%s
        FROM company c JOIN category k USING(domain_id)
        WHERE c.company_id=%s AND k.category_id=%s
        RETURNING reply_id
    """, (owner_id, team_id, data["name"], data["body"],
          actor["company_id"], data["category_id"]))
    row = cursor.fetchone()
    return row[0] if row else None


def update_reply(cursor, actor, reply_id, data):
    cursor.execute("""
        UPDATE quick_reply r SET name=%s,body=%s,domain_id=c.domain_id,
                                 category_id=k.category_id,updated_at=now()
        FROM company c JOIN category k USING(domain_id)
        WHERE r.company_id=c.company_id AND c.company_id=%s AND r.reply_id=%s AND k.category_id=%s
    """, (data["name"], data["body"], actor["company_id"], reply_id, data["category_id"]))
    return cursor.rowcount == 1


def delete_reply(cursor, company_id, reply_id):
    cursor.execute("DELETE FROM quick_reply WHERE company_id=%s AND reply_id=%s", (company_id, reply_id))


def company_teams(cursor, company_id):
    cursor.execute("SELECT team_id,name FROM team WHERE company_id=%s ORDER BY name,team_id", (company_id,))
    return cursor.fetchall()


def active_messenger(cursor, company_id):
    cursor.execute("SELECT messenger_provider FROM company WHERE company_id=%s", (company_id,))
    return cursor.fetchone()[0]


def messenger_routes(cursor, company_id, provider):
    cursor.execute("""
        SELECT d.messenger_channel AS channel, d.team_id, d.staff_user_id, coalesce(t.name,s.name,s.email) AS target_name
        FROM messenger_destination d
        LEFT JOIN team t USING(company_id,team_id)
        LEFT JOIN staff_user s USING(company_id,staff_user_id)
        WHERE d.company_id=%s AND d.provider=%s
        ORDER BY target_name NULLS FIRST,d.team_id,d.staff_user_id
    """, (company_id, provider))
    return fetch_rows(cursor)


def save_messenger_route(cursor, company_id, data):
    provider, channel = data["provider"], data["channel"]
    team = data["target"] if data["destination"] == "team" else None
    staff = data["target"] if data["destination"] == "staff" else None
    if data["destination"] == "default":
        cursor.execute("UPDATE company SET messenger_provider=%s WHERE company_id=%s", (provider, company_id))
    if channel:
        cursor.execute("""
            INSERT INTO messenger_destination(company_id,provider,team_id,staff_user_id,messenger_channel)
            VALUES (%s,%s,%s,%s,%s)
            ON CONFLICT (company_id,provider,team_id,staff_user_id) DO UPDATE SET messenger_channel=excluded.messenger_channel
        """, (company_id, provider, team, staff, channel))
    else:
        cursor.execute("""
            DELETE FROM messenger_destination WHERE company_id=%s AND provider=%s
              AND team_id IS NOT DISTINCT FROM %s::uuid AND staff_user_id IS NOT DISTINCT FROM %s::uuid
        """, (company_id, provider, team, staff))

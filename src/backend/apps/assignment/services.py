from config.database import fetch_rows


def get_policy(cursor, company_uuid, staff_uuid):
    """Task 10 can read the policy with its assignment transaction cursor."""
    cursor.execute("""
        SELECT coalesce(p.enabled,false) AS enabled, coalesce(p.batch_size,1) AS batch_size,
               coalesce(p.refill_mode,'after_all') AS refill_mode
        FROM staff_user s LEFT JOIN staff_assignment_policy p USING(company_uuid,staff_user_uuid)
        WHERE s.company_uuid=%s AND s.staff_user_uuid=%s
    """, (company_uuid, staff_uuid))
    return next(iter(fetch_rows(cursor)), None)


def save_policy(cursor, company_uuid, staff_uuid, policy):
    cursor.execute("""
        INSERT INTO staff_assignment_policy(company_uuid,staff_user_uuid,enabled,batch_size,refill_mode)
        VALUES (%s,%s,%s,%s,%s)
        ON CONFLICT (company_uuid,staff_user_uuid) DO UPDATE SET
            enabled=excluded.enabled,batch_size=excluded.batch_size,refill_mode=excluded.refill_mode
    """, (company_uuid, staff_uuid, policy["enabled"], policy["batch_size"], policy["refill_mode"]))

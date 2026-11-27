"""Executed with kubectl exec inside the demo Django deployment."""
from http.cookiejar import CookieJar
import json
import os
import sys
from urllib.error import HTTPError
from urllib.parse import urlencode, urlparse
from urllib.request import build_opener, HTTPCookieProcessor

from argon2 import PasswordHasher
import psycopg2

COMPANY = "22222222-2222-4222-8222-222222222222"
STAFF = "55555555-5555-4555-8555-555555555555"
EMAIL = "demoagent@canary.example.test"
BASE = "http://127.0.0.1:8000"


def seed(cursor):
    cursor.execute("""
        INSERT INTO staff_user(staff_user_id, company_id, email, password_hash, name, role)
        VALUES (%s,%s,%s,%s,'시연 상담사','agent')
        ON CONFLICT (staff_user_id) DO UPDATE SET password_hash=excluded.password_hash
        WHERE staff_user.company_id=excluded.company_id AND staff_user.email=excluded.email
          AND staff_user.role=excluded.role
        RETURNING staff_user_id
    """, (STAFF, COMPANY, EMAIL, PasswordHasher().hash(os.environ["DEMO_STAFF_PASSWORD"])))
    if not cursor.fetchone():
        raise RuntimeError("Demo account ID already belongs to another employee.")
    cursor.execute("""
        INSERT INTO staff_permission(staff_user_id,permission_id)
        SELECT %s,permission_id FROM permission WHERE code='ticket.read' ON CONFLICT DO NOTHING
    """, (STAFF,))
    cursor.execute("""
        SELECT p.code FROM staff_permission sp JOIN permission p USING(permission_id)
        WHERE sp.staff_user_id=%s
    """, (STAFF,))
    if cursor.fetchall() != [("ticket.read",)]:
        raise RuntimeError("The demo account must have exactly the ticket.read permission.")
    print("Django demo desk ready:", EMAIL)


def verify(cursor):
    cursor.execute("""
        SELECT t.ticket_id::text,t.ticket_number,t.sentiment,c.name FROM ticket t
        JOIN category c ON (c.domain_id,c.category_id)=(t.domain_id,t.category_id)
        JOIN voc.bronze_message b ON b.event_id=t.source_event_id
        WHERE t.company_id=%s AND b.origin='synthetic-canary'
          AND t.sentiment_status='succeeded' AND t.category_status='succeeded'
          AND (SELECT count(DISTINCT j.analysis_kind) FROM voc.classification_job j
               WHERE j.company_id=t.company_id AND j.event_id=t.source_event_id AND j.applied
                 AND j.state='succeeded')=2
        ORDER BY t.created_at DESC,t.ticket_number DESC LIMIT 1
    """, (COMPANY,))
    record = cursor.fetchone()
    if not record:
        raise SystemExit("Run the synthetic canary traffic verification before checking the desk.")
    ticket_id, number, sentiment, category = record
    if not sentiment or not category:
        raise RuntimeError("Classification results are missing from the selected ticket.")
    jar = CookieJar()
    client = build_opener(HTTPCookieProcessor(jar))

    def csrf():
        return next(cookie.value for cookie in jar if cookie.name == "csrftoken")

    def read(path, form=None):
        data = urlencode(form).encode() if form is not None else None
        with client.open(BASE + path, data=data, timeout=15) as response:
            return urlparse(response.url).path, response.read().decode()

    try:
        read("/api/tickets")
    except HTTPError as error:
        if error.code != 401:
            raise
    else:
        raise RuntimeError("Expected anonymous API access to require login.")
    read("/login/")
    path, _ = read("/login/", {"email": EMAIL, "password": os.environ["DEMO_STAFF_PASSWORD"],
                                "csrfmiddlewaretoken": csrf()})
    if path != "/tickets/":
        raise RuntimeError("Authenticated ticket list did not load.")
    try:
        _, payload = read("/api/tickets/" + ticket_id)
        ticket = json.loads(payload)["ticket"]
        expected = {"ticket_uuid": ticket_id, "ticket_number": number, "sentiment": sentiment,
                    "category": category, "sentiment_status": "succeeded", "category_status": "succeeded"}
        if any(ticket.get(key) != value for key, value in expected.items()):
            raise RuntimeError("The authenticated API result differs from PostgreSQL.")
        _, html = read("/tickets/" + ticket_id + "/")
        if any(marker not in html for marker in ("#" + number, f'data-emotion="{sentiment}"',
                                                  f"<span>{category}</span>")):
            raise RuntimeError("Rendered ticket detail differs from the classification results.")
        print(f"Django login and ticket list, detail API and HTML OK: #{number} / {sentiment} / {category}")
    finally:
        read("/logout/", {"csrfmiddlewaretoken": csrf()})


if __name__ == "__main__":
    dsn = os.environ["DATABASE_URL"]
    target = urlparse(dsn)
    if (target.hostname, target.path, target.username) != ("canary-demo-db", "/voc_canary_demo", "voc_demo"):
        raise SystemExit("This script only runs inside the isolated demo desk.")
    with psycopg2.connect(dsn) as db, db.cursor() as cursor:
        cursor.execute("SELECT current_database(), email_domain FROM company WHERE company_id=%s", (COMPANY,))
        if cursor.fetchone() != ("voc_canary_demo", "canary.example.test"):
            raise SystemExit("Prepare the synthetic demo tenant first.")
        {"seed": seed, "verify": verify}[sys.argv[1]](cursor)

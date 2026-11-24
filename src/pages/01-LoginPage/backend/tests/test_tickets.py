"""Test the API against the real PostgreSQL schema in a newly created disposable database."""
import os
import unittest
from contextlib import closing
from importlib import import_module
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import psycopg2
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from psycopg2 import sql
from psycopg2.extensions import make_dsn

create_app = import_module("src.pages.01-LoginPage.backend.main").create_app
ROOT = Path(__file__).resolve().parents[5]


@unittest.skipUnless(os.environ.get("VOC_TICKET_TEST_ADMIN_DSN"), "Set VOC_TICKET_TEST_ADMIN_DSN for PostgreSQL tests")
class TicketTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        admin_dsn = os.environ["VOC_TICKET_TEST_ADMIN_DSN"]
        database = "ticket_api_test_" + uuid4().hex
        with closing(psycopg2.connect(admin_dsn)) as admin, admin.cursor() as cursor:
            admin.autocommit = True
            cursor.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
        cls.addClassCleanup(cls.drop_db, admin_dsn, database)
        cls.dsn = make_dsn(admin_dsn, dbname=database)
        cls.cn = psycopg2.connect(cls.dsn)
        cls.cn.autocommit = True
        cls.addClassCleanup(cls.cn.close)
        cls.cur = cls.cn.cursor()
        cls.addClassCleanup(cls.cur.close)
        for filename in ("schema.sql", "ticket_schema.sql"):
            cls.cur.execute((ROOT / "collector" / filename).read_text())
        password_hash = PasswordHasher().hash("SamplePass1!")
        cls.employees = []
        cls.tickets = []
        for i in (1, 2):
            tenant = f"tenant-{i}"
            company, domain, employee = (str(uuid4()) for _ in range(3))
            cls.cur.execute("INSERT INTO domain(domain_uuid,name) VALUES (%s,%s)", (domain, tenant))
            cls.cur.execute("INSERT INTO company(company_uuid,email_domain,domain_uuid) VALUES (%s,%s,%s)",
                            (company, f"qa{i}.test", domain))
            cls.cur.execute("INSERT INTO staff_user(staff_user_uuid,company_uuid,email,name,password_hash) "
                            "VALUES (%s,%s,%s,'시험 상담사',%s)", (employee, company, f"agent1@qa{i}.test", password_hash))
            cls.cur.execute("INSERT INTO staff_permission SELECT %s,permission_uuid FROM permission WHERE code='ticket.read'",
                            (employee,))
            cls.employees.append(employee)
            if i == 1:
                cls.cur.execute("INSERT INTO team(company_uuid,name) VALUES (%s,'배송팀') RETURNING team_uuid", (company,))
                cls.team = cls.cur.fetchone()[0]
            for ch in ("gmail", "discord"):
                cls.cur.execute("INSERT INTO channel(company_uuid,type,name) VALUES (%s,%s,%s) RETURNING channel_uuid",
                                (company, ch, ch))
                channel = cls.cur.fetchone()[0]
                room = "101" if ch == "discord" else ""
                cls.cur.execute("INSERT INTO voc.ticket_source_connection VALUES(%s,%s,%s,%s,%s,%s)",
                                (tenant, ch, "account", room, company, channel))
                message_count = (2 if i == 1 else 11) if ch == "gmail" else 1
                for num in range(1, message_count + 1):
                    ident = str(uuid4())
                    moment = f"2024-02-{num:02}T16:00:00Z"
                    cls.cur.execute("INSERT INTO voc.bronze_messages VALUES(%s,%s,%s,'account',%s,'qa',%s,'{}','{}')",
                                    (ident, tenant, ch, str(num), moment))
                    cls.cur.execute("""
                        INSERT INTO voc.silver_messages(schema_version,event_id,company_id,channel,connection_id,
                            source_message_id,source_conversation_id,source_channel_id,message_kind,sender_ref,
                            body_text,body_version,received_at,source_metadata)
                        VALUES (4,%s,%s,%s,'account',%s,'thread',%s,'customer_inquiry','customer_10',%s,1,%s,%s)
                        """, (ident, tenant, ch, str(num), room or None, '배송 요청 '+str(num), moment,
                              '{}' if room else '{"label_ids":[],"to_addresses":[]}'))
                    cls.cur.execute("SELECT ticket_uuid FROM ticket WHERE source_event_id=%s", (ident,))
                    cls.tickets.append(cls.cur.fetchone()[0])

        a, b, discord, *_ = cls.tickets
        cls.cur.execute("UPDATE ticket SET assigned_team_uuid=%s WHERE ticket_uuid IN (%s,%s)", (cls.team,a,b))
        cls.cur.execute("""
            UPDATE ticket SET assignee_uuid=%s,status='보류',sentiment='부정',sentiment_status='succeeded'
            WHERE ticket_uuid=%s""", (cls.employees[0],b))
        cls.cur.execute("INSERT INTO ticket_duplicate_candidate(ticket_uuid,candidate_ticket_uuid) VALUES (%s,%s)",
                        sorted((a,discord)))
        with patch.dict(os.environ, {"DATABASE_URL": cls.dsn}):
            cls.app = create_app()

    @staticmethod
    def drop_db(dsn, name):
        with closing(psycopg2.connect(dsn)) as admin, admin.cursor() as cursor:
            admin.autocommit = True
            cursor.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))

    def setUp(self):
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)

    def login(self, company=1):
        response = self.client.post("/api/auth/login", json={"email": f"agent1@qa{company}.test", "password": "SamplePass1!"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("HttpOnly", response.headers["set-cookie"])

    def test_identity_tenant_permissions_and_logout(self):
        self.assertEqual(self.client.get("/api/tickets").status_code, 401)
        self.login()
        self.assertEqual(self.client.get("/api/auth/me").json()["email"], "agent1@qa1.test")
        self.assertEqual(self.client.get(f"/api/tickets/{self.tickets[-1]}").status_code, 404)
        self.cur.execute("DELETE FROM staff_permission WHERE staff_user_uuid=%s", (self.employees[0],))
        try:
            for path in ("/api/tickets", "/api/tickets/options", f"/api/tickets/{self.tickets[0]}"):
                self.assertEqual(self.client.get(path).status_code, 403)
        finally:
            self.cur.execute("INSERT INTO staff_permission SELECT %s,permission_uuid FROM permission WHERE code='ticket.read'",
                             (self.employees[0],))
        issued_cookie = self.client.cookies.get("voc_session")
        self.assertEqual(self.client.post("/api/auth/logout").status_code, 204)
        self.client.cookies.set("voc_session", issued_cookie)
        self.assertEqual(self.client.get("/api/tickets").status_code, 401)

    def test_stable_pages_and_total_after_last_page(self):
        self.login(company=2)
        first = self.client.get("/api/tickets", params={"page_size": 10}).json()
        last = self.client.get("/api/tickets", params={"page_size": 10, "page": 99}).json()
        self.assertEqual((first["total"], last["total"], last["page"]), (12, 12, 2))
        self.assertEqual(len(first["items"]), 10)
        self.assertEqual(len(last["items"]), 2)
        seen = [row["ticket_uuid"] for row in first["items"] + last["items"]]
        self.assertEqual(len(set(seen)), 12)
        desc = self.client.get("/api/tickets", params={"sort": "received-desc"}).json()
        self.assertEqual(desc["items"][0]["ticket_uuid"], seen[-1])

    def test_independent_filters_search_and_kst_calendar(self):
        self.login()
        def items(**params):
            response = self.client.get("/api/tickets", params=params)
            self.assertEqual(response.status_code, 200, response.text)
            return [row["ticket_uuid"] for row in response.json()["items"]]

        a, b, discord, *_ = self.tickets
        self.assertEqual(items(), [a, discord, b])
        self.assertEqual(items(team=self.team,assignee="unassigned"), [a])
        self.assertEqual(items(scope="mine",status="보류",sentiment="부정"), [b])
        self.assertEqual(items(start="2024-02-02", end="2024-02-02"), [a, discord])
        self.assertEqual(items(search_by="id",q="#00000002"), [b])
        self.assertEqual(items(search_by="customer",q="_10"), [a, discord, b])
        self.assertEqual(items(search_by="customer",q="%"), [])
        for params in ({"q":"가"*51}, {"days":2}, {"start":"2024-01-01","end":"2024-12-31"},
                       {"start":"2024-01-01"}, {"assignee":"bad"}, {"sort":"x"}):
            self.assertEqual(self.client.get("/api/tickets",params=params).status_code, 422)
        options = self.client.get("/api/tickets/options").json()
        self.assertEqual([item["id"] for item in options["assignees"]], [self.employees[0]])
        self.assertEqual(options["teams"], [{"id":self.team,"name":"배송팀"}])

    def test_customer_timeline_and_bidirectional_candidate_visibility(self):
        a,b,discord,*_ = self.tickets
        self.login()
        def detail(ident):
            response = self.client.get(f"/api/tickets/{ident}")
            self.assertEqual(response.status_code, 200, response.text)
            return response.json()
        data = detail(a)
        self.assertEqual([m["ticket_uuid"] for m in data["messages"]], [a,b])
        self.assertEqual([c["ticket_uuid"] for c in data["duplicate_candidates"]], [discord])
        self.assertEqual([c["ticket_uuid"] for c in detail(discord)["duplicate_candidates"]], [a])
        self.assertEqual(detail(b)["duplicate_candidates"], [])
        with self.assertRaises(psycopg2.Error):
            self.cur.execute("INSERT INTO ticket_duplicate_candidate VALUES (%s,%s)", sorted((a,self.tickets[-1])))
        self.assertEqual([row["ticket_uuid"] for row in detail(a)["duplicate_candidates"]], [discord])
        self.assertEqual(data["ticket"]["sentiment_status"], "pending")
        self.assertIsNone(data["ticket"]["sentiment"])
        self.assertEqual(self.client.get(f"/api/tickets/{self.tickets[-1]}").status_code, 404)
        self.cur.execute("UPDATE voc.silver_messages SET message_kind='unknown' WHERE event_id="
                         "(SELECT source_event_id FROM ticket WHERE ticket_uuid=%s)", (discord,))
        try:
            self.assertEqual(detail(a)["duplicate_candidates"], [])
        finally:
            self.cur.execute("UPDATE voc.silver_messages SET message_kind='customer_inquiry' WHERE event_id="
                             "(SELECT source_event_id FROM ticket WHERE ticket_uuid=%s)", (discord,))

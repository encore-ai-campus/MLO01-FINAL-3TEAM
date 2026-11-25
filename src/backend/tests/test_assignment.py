"""Exercise category assignment with the existing PostgreSQL ticket schema."""
import os
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

import psycopg2
from argon2 import PasswordHasher
from django.test import Client, SimpleTestCase, override_settings
from psycopg2 import sql
from psycopg2.extensions import make_dsn

from tests.database import use_database


RESULT_URL = "/api/internal/ticket-assignments/category-result"
TEST_TOKEN = "test-assignment-worker"


class WorkerEndpointTests(SimpleTestCase):
    @override_settings(VOC_ASSIGNMENT_WORKER_TOKEN=TEST_TOKEN)
    def test_invalid_auth_and_missing_category_never_apply(self):
        client = Client(enforce_csrf_checks=True)
        with patch("apps.assignment.presentation.views.apply_category_result") as apply:
            self.assertEqual(client.post(RESULT_URL, {}, content_type="application/json").status_code, 403)
            for body in ({}, {"category_uuid": []}, {"category_uuid": ""}):
                response = client.post(RESULT_URL, body, content_type="application/json",
                                       HTTP_AUTHORIZATION=f"Bearer {TEST_TOKEN}")
                self.assertEqual(response.status_code, 422)
            apply.assert_not_called()


@unittest.skipUnless(os.environ.get("VOC_TICKET_TEST_ADMIN_DSN"), "Set VOC_TICKET_TEST_ADMIN_DSN for PostgreSQL tests")
class AssignmentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.enterClassContext(override_settings(VOC_ASSIGNMENT_WORKER_TOKEN=TEST_TOKEN))
        create_dsn = os.environ["VOC_TICKET_TEST_ADMIN_DSN"]
        name = "assignment_test_" + uuid4().hex
        cls.create_database(create_dsn, name, "CREATE DATABASE {}")
        cls.addClassCleanup(cls.create_database, create_dsn, name, "DROP DATABASE {}")
        cls.dsn = make_dsn(create_dsn, dbname=name)
        cls.cn = psycopg2.connect(cls.dsn)
        cls.cn.autocommit = True
        cls.addClassCleanup(cls.cn.close)
        cls.cursor = cls.cn.cursor()
        cls.addClassCleanup(cls.cursor.close)
        collector = Path(__file__).resolve().parents[2] / "collector"
        for schema in ("schema.sql", "ticket_schema.sql", "settings_schema.sql"):
            cls.cursor.execute((collector / schema).read_text(encoding="utf-8"))
        cls.enterClassContext(use_database(cls.dsn))

    @staticmethod
    def create_database(dsn, name, statement):
        with closing(psycopg2.connect(dsn)) as cn, cn.cursor() as cursor:
            cn.autocommit = True
            cursor.execute(sql.SQL(statement).format(sql.Identifier(name)))

    def insert(self, sql, *params):
        self.cursor.execute(sql, params)
        value = self.cursor.fetchone()[0]
        return str(value) if isinstance(value, UUID) else value

    def setUp(self):
        self.client = Client()
        self.tenant = "qa" + uuid4().hex
        self.email = f"admin@{self.tenant}.test"
        self.domain = self.insert("INSERT INTO domain(name) VALUES ('업종') RETURNING domain_uuid")
        self.company = self.insert("""
            INSERT INTO company(email_domain,domain_uuid) VALUES (%s,%s) RETURNING company_uuid
        """, f"{self.tenant}.test", self.domain)
        self.channel = self.insert("""
            INSERT INTO channel(company_uuid,type,name) VALUES (%s,'gmail','Gmail') RETURNING channel_uuid
        """, self.company)
        self.cursor.execute("INSERT INTO voc.ticket_source_connection VALUES(%s,'gmail','account','',%s,%s)",
                            (self.tenant, self.company, self.channel))
        self.teams = [self.insert("INSERT INTO team(company_uuid,name) VALUES (%s,%s) RETURNING team_uuid",
                                  self.company, name) for name in ("배송", "환불")]
        self.categories = [self.insert("INSERT INTO category(domain_uuid,name) VALUES (%s,%s) RETURNING category_uuid",
                                       self.domain, name) for name in ("배송 지연", "환불 지연", "기타")]
        self.staff = []
        for team, email in zip(self.teams, (self.email, f"agent@{self.tenant}.test")):
            self.staff.append(self.insert("""
                INSERT INTO staff_user(company_uuid,team_uuid,email,password_hash,role)
                VALUES (%s,%s,%s,%s,'admin') RETURNING staff_user_uuid
            """, self.company, team, email, PasswordHasher().hash("TestPass12!")))
        for employee in self.staff:
            self.cursor.execute("""
                INSERT INTO staff_permission SELECT %s,permission_uuid FROM permission
                WHERE code IN ('ticket.read','ticket.handle')
            """, (employee,))
            self.cursor.execute("INSERT INTO staff_assignment_policy VALUES (%s,%s,true,1,'after_all')",
                                (self.company, employee))
        self.assertEqual(self.client.post("/api/auth/login", {"email": self.email, "password": "TestPass12!"},
                                          content_type="application/json").status_code, 200)
        for category, team in zip(self.categories, self.teams):
            response = self.client.post("/settings/assignment/category-routes/", {
                "category_uuid": category, "team_uuid": team,
            })
            self.assertEqual(response.status_code, 302, response.content)

    def ticket(self):
        event_id = str(uuid4())
        self.cursor.execute("INSERT INTO voc.bronze_messages VALUES(%s,%s,'gmail','account',%s,'qa',now(),'{}','{}')",
                            (event_id, self.tenant, event_id))
        self.cursor.execute("""
            INSERT INTO voc.silver_messages(schema_version,event_id,company_id,channel,connection_id,
                source_message_id,source_conversation_id,message_kind,sender_ref,body_text,body_version,received_at,source_metadata)
            VALUES (4,%s,%s,'gmail','account',%s,'thread','customer_inquiry','customer_qa','배송 상담',1,now(),
                   '{"label_ids":[],"to_addresses":[]}')
        """, (event_id, self.tenant, event_id))
        ticket_id = self.insert("SELECT ticket_uuid FROM ticket WHERE source_event_id=%s", event_id)
        return event_id, ticket_id

    def result(self, event_id, category_uuid, body_version=1, status=200):
        response = self.client.post(RESULT_URL, {
            "company_uuid": self.company, "event_id": event_id, "category_uuid": category_uuid,
            "preprocess_version": 1, "body_version": body_version,
        }, content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {TEST_TOKEN}")
        self.assertEqual(response.status_code, status, response.content)
        return response.json()

    def test_assignment_duplicate_and_manual_change(self):
        event, ticket = self.ticket()
        wait_event, wait_ticket = self.ticket()
        original = self.result(event, self.categories[0])
        self.assertEqual(original["assigned_team_uuid"], self.teams[0])
        self.assertEqual(original["assignee_uuid"], self.staff[0])
        self.assertEqual(self.result(event, self.categories[0])["outcome"], "already_applied")
        self.result(event, self.categories[1], status=409)
        assigned_count = self.insert("""
            SELECT count(*) FROM ticket_event JOIN event_type USING (event_type_uuid)
            WHERE ticket_uuid=%s AND code='assigned'
        """, ticket)
        self.assertEqual(assigned_count, 1)
        self.assertIsNone(self.result(wait_event, self.categories[0])["assignee_uuid"])

        url = f"/api/tickets/{ticket}/category"
        payload = {"category_uuid": self.categories[1], "reason": "카테고리 확인"}
        response = self.client.post(url, payload, content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["category_uuid"], self.categories[1])
        self.assertEqual(response.json()["assigned_team_uuid"], self.teams[0])
        self.assertEqual(response.json()["assignee_uuid"], self.staff[0])
        retry = self.client.post(url, payload, content_type="application/json")
        self.assertEqual(retry.json()["outcome"], "already_applied")
        self.assertEqual(self.insert("""
            SELECT count(*) FROM ticket_event JOIN event_type USING (event_type_uuid)
            WHERE ticket_uuid=%s AND code='ticket_category_changed' AND actor_uuid=%s
        """, ticket, self.staff[0]), 1)

        detail_url = f"/tickets/{ticket}/"
        response = self.client.post(detail_url + "category/", {"category_uuid": self.categories[2]})
        self.assertEqual((response.status_code, response.url), (302, detail_url))
        self.cursor.execute("""
            SELECT category_uuid::text,assigned_team_uuid::text,assignee_uuid::text,category_review_reason
            FROM ticket WHERE ticket_uuid=%s
        """, (ticket,))
        self.assertEqual(self.cursor.fetchone(), (self.categories[2], self.teams[0], self.staff[0], None))
        invalid = self.client.post(url, {"category_uuid": str(uuid4())}, content_type="application/json")
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(self.result(event, self.categories[0])["outcome"], "manual_preserved")
        self.assertIsNone(self.insert("SELECT assignee_uuid FROM ticket WHERE ticket_uuid=%s", wait_ticket))

    def test_abstention_missing_route_stale_and_completion_refill(self):
        event, ticket = self.ticket()
        self.cursor.execute("UPDATE voc.silver_messages SET body_version=2 WHERE event_id=%s", (event,))
        self.assertEqual(self.result(event, self.categories[0])["outcome"], "stale_result")
        self.assertEqual(self.result(event, None, 2)["review_reason"], "classification_abstained")
        self.assertIsNone(self.insert("SELECT assigned_team_uuid FROM ticket WHERE ticket_uuid=%s", ticket))
        manual = self.client.post(f"/api/tickets/{ticket}/category", {"category_uuid": self.categories[0]},
                                  content_type="application/json")
        self.assertEqual(manual.status_code, 200, manual.content)
        self.assertIsNone(manual.json()["assigned_team_uuid"])
        self.assertIsNone(manual.json()["assignee_uuid"])
        self.assertIsNone(manual.json()["review_reason"])
        other_event, _ = self.ticket()
        self.assertEqual(self.result(other_event, self.categories[2])["review_reason"], "no_team_mapping")

        event, ticket = self.ticket()
        self.result(event, self.categories[0])
        wait_event, waiting = self.ticket()
        self.assertIsNone(self.result(wait_event, self.categories[0])["assignee_uuid"])
        with patch("apps.tickets.delivery.send_reply", return_value="test-message-id"):
            response = self.client.post(f"/api/tickets/{ticket}/messages", {
                "message_type": "reply", "body": "배송 안내", "request_key": str(uuid4()),
            }, content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.insert("SELECT assignee_uuid FROM ticket WHERE ticket_uuid=%s", waiting),
                         self.staff[0])

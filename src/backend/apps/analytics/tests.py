import os
from contextlib import closing
from datetime import datetime, timezone
from unittest import TestCase, skipUnless
from unittest.mock import patch
from uuid import uuid4

import psycopg2
from argon2 import PasswordHasher
from django.test import Client
from psycopg2 import sql
from psycopg2.extensions import make_dsn

from tests.database import use_database


NOW = datetime(2026, 11, 24, 6, tzinfo=timezone.utc)
PASSWORD = "AnalyticsFixture1!"


@skipUnless(os.environ.get("VOC_ANALYTICS_TEST_DSN"), "Set VOC_ANALYTICS_TEST_DSN for PostgreSQL tests")
class AnalyticsPageTests(TestCase):
    def setUp(self):
        base_dsn = os.environ["VOC_ANALYTICS_TEST_DSN"]
        schema = "analytics_test_" + uuid4().hex
        with closing(psycopg2.connect(base_dsn)) as cn, cn.cursor() as cursor:
            cn.autocommit = True
            cursor.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        self.addCleanup(self.drop_schema, base_dsn, schema)

        dsn = make_dsn(base_dsn, options=f"-c search_path={schema}")
        cn = psycopg2.connect(dsn)
        cn.autocommit = True
        self.addCleanup(cn.close)
        self.cur = self.enterContext(cn.cursor())
        self.cur.execute("""
            CREATE TABLE company (company_uuid uuid PRIMARY KEY, email_domain text);
            CREATE TABLE team (team_uuid uuid PRIMARY KEY, company_uuid uuid, name text);
            CREATE TABLE staff_user (
                staff_user_uuid uuid PRIMARY KEY, company_uuid uuid, team_uuid uuid,
                name text, email text, password_hash text, role text
            );
            CREATE TABLE staff_session (token_hash text PRIMARY KEY, staff_user_uuid uuid, expires_at timestamptz);
            CREATE TABLE permission (permission_uuid uuid PRIMARY KEY, code text);
            CREATE TABLE staff_permission (staff_user_uuid uuid, permission_uuid uuid);
            CREATE TABLE category (category_uuid uuid PRIMARY KEY, domain_uuid uuid, name text);
            CREATE TABLE ticket (
                ticket_uuid uuid PRIMARY KEY, company_uuid uuid, assigned_team_uuid uuid, assignee_uuid uuid,
                category_uuid uuid, domain_uuid uuid, status text, sentiment text, created_at timestamptz, visible boolean
            );
            CREATE VIEW visible_ticket AS SELECT * FROM ticket WHERE visible;
            CREATE TABLE ticket_message (
                ticket_message_uuid uuid PRIMARY KEY, ticket_uuid uuid, staff_user_uuid uuid,
                sender_type text, message_type text
            );
        """)
        self.company, self.other, self.team, self.other_team, self.domain, self.category = (str(uuid4()) for _ in range(6))
        for company, team, email_domain, team_name in (
            (self.company, self.team, "qa.test", "배송팀"),
            (self.other, self.other_team, "other.test", "외부팀"),
        ):
            self.cur.execute("INSERT INTO company VALUES (%s,%s)", (company, email_domain))
            self.cur.execute("INSERT INTO team VALUES (%s,%s,%s)", (team, company, team_name))
        self.admin = self.staff(self.company, self.team, "admin1@qa.test", "테스트 관리자", "admin")
        self.agent = self.staff(self.company, self.team, "agent1@qa.test", "테스트 상담사", "counselor")
        self.foreign = self.staff(self.other, self.other_team, "agent1@other.test", "외부 상담사", "counselor")
        self.cur.execute("INSERT INTO category VALUES (%s,%s,'배송 지연')", (self.category, self.domain))
        self.solved = self.ticket("2026-11-24T08:00:00+09", "부정", "답변완료", assignee=self.agent)
        self.held = self.ticket("2026-11-22T09:00:00+09", "매우 부정", "보류", assignee=self.agent)
        self.ticket("2026-11-24T13:00:00+09", None, "미처리", category=False)
        self.past = self.ticket("2026-11-17T23:59:59+09", "긍정", "답변완료", assignee=self.agent)
        self.ticket("2026-11-24T08:00:00+09", "긍정", "미처리", visible=False)
        self.ticket("2026-11-24T08:00:00+09", "긍정", "미처리", other=True, assignee=self.foreign)
        self.enterContext(use_database(dsn))
        self.enterContext(patch("apps.analytics.services.timezone.now", return_value=NOW))
        self.client = Client()

    @staticmethod
    def drop_schema(dsn, schema):
        with closing(psycopg2.connect(dsn)) as cn, cn.cursor() as cursor:
            cn.autocommit = True
            cursor.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))

    def staff(self, company, team, email, name, role):
        uid = str(uuid4())
        self.cur.execute("INSERT INTO staff_user VALUES (%s,%s,%s,%s,%s,%s,%s)",
                         (uid, company, team, name, email, PasswordHasher().hash(PASSWORD), role))
        return uid

    def ticket(self, created, sentiment, status, assignee=None, category=True, visible=True, other=False):
        uid = str(uuid4())
        self.cur.execute("INSERT INTO ticket VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", (
            uid, self.other if other else self.company, self.other_team if other else self.team, assignee,
            self.category if category else None, self.domain, status, sentiment, created, visible,
        ))
        return uid

    def login(self, email="admin1@qa.test"):
        response = self.client.post("/api/auth/login", {"email": email, "password": PASSWORD}, content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content)

    def events(self):
        self.cur.execute("""
            CREATE TABLE event_type (event_type_uuid uuid PRIMARY KEY, code text);
            CREATE TABLE ticket_event (
                ticket_event_uuid uuid PRIMARY KEY, ticket_uuid uuid, event_type_uuid uuid, actor_uuid uuid,
                recipient_uuid uuid, from_status text, to_status text, created_at timestamptz
            );
            ALTER TABLE ticket_message ADD COLUMN sent_at timestamptz;
            CREATE TABLE collab_request (
                collab_request_uuid uuid PRIMARY KEY, ticket_uuid uuid, recipient_uuid uuid,
                request_type text, status text
            );
        """)
        kinds = {code: str(uuid4()) for code in ("assigned", "ticket_transfer", "answer_completed")}
        for code, uid in kinds.items():
            self.cur.execute("INSERT INTO event_type VALUES (%s,%s)", (uid, code))
        for ticket, code, time in (
            (self.solved, "assigned", "2026-11-24T08:01:00+09"),
            (self.solved, "assigned", "2026-11-24T08:02:00+09"),
            (self.solved, "answer_completed", "2026-11-24T10:00:00+09"),
            (self.solved, "answer_completed", "2026-11-24T10:05:00+09"),
            (self.held, "ticket_transfer", "2026-11-22T09:01:00+09"),
            (self.past, "answer_completed", "2026-11-17T23:59:59+09"),
        ):
            complete = code == "answer_completed"
            self.cur.execute("INSERT INTO ticket_event VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                             (str(uuid4()), ticket, kinds[code], self.agent, None if complete else self.agent,
                              "미처리" if complete else None, "답변완료" if complete else None, time))
        for mode, time in (
            ("memo", "2026-11-24T08:08:00+09"),
            ("reply", None), ("reply", "2026-11-24T09:00:00+09"), ("reply", "2026-11-24T09:40:00+09"),
        ):
            self.cur.execute("INSERT INTO ticket_message VALUES (%s,%s,%s,'staff',%s,%s)",
                             (str(uuid4()), self.solved, self.agent, mode, time))
        for state in ("pending", "accepted", "declined", "replied"):
            self.cur.execute("INSERT INTO collab_request VALUES (%s,%s,%s,'review',%s)",
                             (str(uuid4()), self.solved, self.agent, state))

    def test_filters_receipts_sentiment_category_and_hour_groups(self):
        self.login()
        response = self.client.get("/analytics/", {"scope": self.team})
        self.assertEqual(response.status_code, 200, response.content)
        metrics = {label: value for label, value, _, _ in response.context["metrics"]}
        self.assertEqual(metrics, {"신규 접수": 3, "처리 완료": "—", "24시간 이상 미처리": 1,
                                  "부정·매우 부정 문의 비율": 100.0})
        self.assertEqual(response.context["sentiments"], [
            ("긍정", 0, 0.0), ("중립", 0, 0.0), ("부정", 1, 50.0), ("매우 부정", 1, 50.0)])
        self.assertEqual(response.context["categories"], [("배송 지연", 2, 66.7), ("미분류", 1, 33.3)])
        self.assertEqual(response.context["category_increases"], [("배송 지연", 1, 2, 1)])
        hourly = response.context["hourly"]["table"]
        self.assertEqual(len(hourly), 24)
        self.assertEqual((hourly[8], hourly[23]), (("08시", 1, 0), ("23시", 0, 1)))
        self.assertEqual(response.context["pending"]["total"], 2)
        self.assertNotIn("CSAT", response.content.decode())
        self.assertNotIn("분석 실패", response.content.decode())
        previous = self.client.get("/analytics/", {"period": "previous"})
        self.assertEqual(previous.context["metrics"][0][1], 1)

    def test_counselor_history_comparison_and_scope(self):
        self.events()
        self.login()
        listing = self.client.get("/analytics/", {"scope": self.team, "tab": "agents"})
        self.assertEqual([agent["id"] for agent in listing.context["agents"]], [self.agent])
        self.assertNotIn("오늘 할당", listing.content.decode())
        detail = self.client.get("/analytics/", {"tab": "agents", "scope": self.team, "agent": self.agent})
        self.assertEqual(detail.status_code, 200, detail.content)
        data = detail.context["agent"]
        self.assertEqual((data["assigned"], data["done"], data["previous_done"]), (2, 1, 1))
        self.assertEqual((data["pending_assigned"], data["pending_transferred"], data["review_pending"]), (0, 1, 2))
        self.assertEqual(data["first_answer"], "1시간")
        overview = self.client.get("/analytics/", {"scope": self.team})
        self.assertEqual(overview.context["latency"][1][2], "2시간")
        self.assertEqual(self.client.get("/analytics/", {"tab": "agents", "agent": self.foreign}).status_code, 404)
        self.assertEqual(self.client.get("/analytics/", {"scope": self.other_team}).status_code, 400)

    def test_non_admin_and_missing_role_fail_closed(self):
        self.assertEqual(self.client.get("/analytics/").status_code, 302)
        self.login("agent1@qa.test")
        self.assertEqual(self.client.get("/analytics/", {"preview": "administrator"}).status_code, 403)
        self.login()
        self.cur.execute("UPDATE staff_user SET role = 'counselor' WHERE staff_user_uuid = %s", (self.admin,))
        self.assertEqual(self.client.get("/analytics/").status_code, 403)
        self.cur.execute("ALTER TABLE staff_user DROP COLUMN role")
        self.assertEqual(self.client.get("/analytics/").status_code, 403)

    def test_dashboard_access_follows_employee_grant_instead_of_role_after_settings_setup(self):
        permission = str(uuid4())
        self.cur.execute("INSERT INTO permission VALUES (%s,'analytics.read')", (permission,))
        self.cur.execute("INSERT INTO staff_permission VALUES (%s,%s)", (self.agent, permission))
        unrelated_team = str(uuid4())
        self.cur.execute("INSERT INTO team VALUES (%s,%s,'같은 회사 다른 팀')", (unrelated_team, self.company))
        self.login("agent1@qa.test")
        default = self.client.get("/analytics/")
        self.assertEqual(default.status_code, 200)
        self.assertEqual(default.context["scope"], self.team)
        self.assertEqual(self.client.get("/analytics/", {"scope": "org"}).status_code, 400)
        self.assertEqual(self.client.get("/analytics/", {"scope": unrelated_team}).status_code, 400)
        self.assertEqual(self.client.get("/analytics/", {"scope": self.other_team}).status_code, 400)
        self.assertEqual(self.client.get("/analytics/", {"tab": "agents", "agent": self.agent}).status_code, 403)
        self.cur.execute("UPDATE staff_user SET role='team_lead' WHERE staff_user_uuid=%s", (self.agent,))
        self.assertEqual(self.client.get("/analytics/", {"tab": "agents"}).status_code, 200)
        self.login()
        self.assertEqual(self.client.get("/analytics/").status_code, 403)
